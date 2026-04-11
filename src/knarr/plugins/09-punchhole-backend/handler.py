"""
Punchhole Cache Backend Plugin — LAN side.

Reads internal DB, applies granularity controls, signs cache objects, builds
ACL, and pushes everything to the frontend via the bus.

Responsibilities:
  - Load exposure_schema.toml on startup.
  - Resolve ACL shorthands (all_signed / known_hosts / peer / trusted / list).
  - Build and sign cache objects per schema.
  - Build and sign punchhole cards per requester tier.
  - Handle live_query objects (economy.bilateral) on-demand per requester.
  - Watch internal bus events and emit cache.stale.* when source data changes.
  - Emit cache.backend.ready after warm-start push completes.

Airgap invariants:
  - All signing happens here (ctx.sign_document). Frontend never signs.
  - All DB reads happen here. Frontend never touches storage.
  - Config lives here. Frontend receives it via bus events only.

Bus subscriptions:
  cache.miss.*    — on-demand cache fills from frontend
  credit.change   — stale: economy.summary, economy.bilateral
  receipt.issued  — stale: economy.summary
  skill.registered / skill.removed — stale: skills

Bus emissions:
  cache.fill.{object_key}     — signed cache object per (object_key, acl_group)
  cache.fill.acl.{group}      — ACL node list per group (for frontend ACL map)
  cache.stale.{object_key}    — signals frontend to mark entries stale
  cache.backend.ready         — startup complete
"""

import asyncio
import json
import logging
import math
import sqlite3
import time
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from knarr.dht.plugins import PluginContext, PluginHooks, NodeHealth
from knarr.core.models import NodeInfo

log = logging.getLogger("knarr.plugin.punchhole-backend")

# ---------------------------------------------------------------------------
# Granularity helpers
# ---------------------------------------------------------------------------

def _apply_granularity(value: Any, control: str) -> Any:
    """Apply a single granularity control to a value.

    range:N always rounds DOWN (floor division). Never overstates.
    NaN/Inf rejected on numeric controls — returns None.
    """
    if control == "exact":
        return value

    if control == "boolean":
        return bool(value)

    if control == "hidden":
        return None  # caller must drop the field

    if control == "age":
        # Expect a float Unix timestamp or ISO string; return "Xh ago"
        try:
            if isinstance(value, str):
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(value.rstrip("Z")).replace(tzinfo=timezone.utc)
                epoch = dt.timestamp()
            else:
                epoch = float(value)
            hours = max(0, int((time.time() - epoch) / 3600))
            return f"{hours}h ago"
        except Exception:
            return "unknown"

    if control == "list":
        # Names/IDs only — strip internal fields
        if isinstance(value, list):
            result = []
            for item in value:
                if isinstance(item, dict):
                    result.append({k: v for k, v in item.items()
                                   if k in ("name", "id", "skill_name", "node_id", "price", "visibility")})
                else:
                    result.append(str(item))
            return result
        return value

    if control.startswith("recent:"):
        try:
            n = int(control.split(":", 1)[1])
            if isinstance(value, list):
                return value[-n:] if n > 0 else []
            return value
        except (ValueError, IndexError):
            return value

    if control.startswith("range:"):
        try:
            n_str = control.split(":", 1)[1]
            n = float(n_str)
            if not math.isfinite(n) or n <= 0:
                return None  # Invalid range control — suppress value
            if isinstance(value, float):
                if not math.isfinite(value):
                    return None
                return math.floor(value / n) * n
            if isinstance(value, int):
                return (value // int(n)) * int(n)
            return value
        except (ValueError, ZeroDivisionError):
            return value

    # Unknown control — return exact
    return value


def _build_data_dict(raw: Dict[str, Any], fields: List[str],
                     granularity: Dict[str, str]) -> Dict[str, Any]:
    """Apply granularity controls to a raw data dict, returning disclosure-safe copy."""
    out = {}
    for field in fields:
        if field not in raw:
            continue
        control = granularity.get(field, "exact")
        if control == "hidden":
            continue
        val = _apply_granularity(raw[field], control)
        if val is None:
            continue  # Drop None values (hidden, NaN guard, or invalid input)
        out[field] = val
    return out


# ---------------------------------------------------------------------------
# Schema loader
# ---------------------------------------------------------------------------

def _load_schema(schema_path: Path) -> Dict[str, Any]:
    """Load and validate exposure_schema.toml. Returns schema dict."""
    try:
        raw = tomllib.loads(schema_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning(f"punchhole-backend: schema not found at {schema_path} — using empty schema")
        return {"objects": {}, "trusted_nodes": []}
    except Exception as exc:
        log.error(f"punchhole-backend: schema load error: {exc}")
        return {"objects": {}, "trusted_nodes": []}

    objects = raw.get("objects", {})
    trusted_nodes = raw.get("trusted_nodes", [])
    return {"objects": objects, "trusted_nodes": trusted_nodes}


# ---------------------------------------------------------------------------
# ACL tier ordering (higher index = more privileged)
# ---------------------------------------------------------------------------
_TIER_ORDER = ["all_signed", "trusted", "known_hosts", "peer", "private"]


def _tier_index(tier: str) -> int:
    try:
        return _TIER_ORDER.index(tier)
    except ValueError:
        return -1


def _tier_has_access(requester_tier: str, required: str) -> bool:
    """Return True if requester_tier satisfies the required access level."""
    # all_signed is the broadest (least privileged) — everyone has it.
    # peer is the most privileged in base schema.
    # Unknown tiers fail closed — deny access if either tier is unrecognized.
    req_idx = _tier_index(requester_tier)
    needed_idx = _tier_index(required)
    if req_idx < 0 or needed_idx < 0:
        return False
    return req_idx >= needed_idx


# ---------------------------------------------------------------------------
# Backend plugin
# ---------------------------------------------------------------------------

class PunchholeBackendPlugin(PluginHooks):
    """
    Punchhole Backend — reads internal DB, signs cache objects, pushes to frontend.
    """

    def __init__(self, ctx: PluginContext, config: dict):
        self._ctx = ctx
        self._config = config
        self._debug = config.get("debug", False)
        # PREREQ-01: punchhole_epoch — increments each time any cache object is staled.
        # Enables event-driven indexer re-crawl in future sprints.
        self._punchhole_epoch: int = 0

        # PREREQ-03: indexer shorthand — maps indexer name -> node_id hex.
        # Populated from config section [exposure.indexers] or config key "indexers".
        self._indexers: Dict[str, str] = {}
        raw_indexers = config.get("indexers", {})
        if isinstance(raw_indexers, dict):
            self._indexers = {k: str(v) for k, v in raw_indexers.items()}

        # Load schema
        schema_file = config.get("schema_file", "exposure_schema.toml")
        schema_path = ctx.plugin_dir / schema_file
        schema_data = _load_schema(schema_path)
        self._objects: Dict[str, Any] = schema_data["objects"]
        self._trusted_nodes: List[str] = schema_data["trusted_nodes"]
        self._cache: Dict[tuple[str, str], dict] = {}
        self._started = False

        # Subscribe to relevant bus events
        if ctx.subscribe_events:
            self._miss_sub = ctx.subscribe_events("cache.miss.*")
            self._internal_sub = ctx.subscribe_events(
                "credit.change",
                "receipt.issued",
                "skill.registered",
                "skill.removed",
                "configuration.order",
            )
            asyncio.ensure_future(self._miss_loop())
            asyncio.ensure_future(self._stale_loop())
        else:
            log.warning("punchhole-backend: subscribe_events not available")
            self._miss_sub = None
            self._internal_sub = None

        # Warm start — run after event loop is running
        asyncio.ensure_future(self._startup())

        log.info("punchhole-backend: initialized")

    # ------------------------------------------------------------------
    # Startup sequence
    # ------------------------------------------------------------------

    async def _startup(self):
        """
        Startup sequence (spec §B2d):
        1. Subscribe to cache.miss.* (done in __init__)
        2. Build all cache objects proactively
        3. Push ACL config to frontend
        4. Emit cache.backend.ready
        """
        if self._started:
            return
        self._started = True
        # Small yield to let the event loop stabilise
        await asyncio.sleep(0)

        try:
            # Resolve ACL for all tiers and push to frontend (C-05)
            await self._push_acl_config()

            # Build and emit all non-live objects
            for object_key, obj_config in self._objects.items():
                if obj_config.get("live_query", False):
                    # Bilateral/live objects are built on demand
                    continue
                for acl_group in _TIER_ORDER:
                    if _tier_has_access(acl_group, obj_config.get("access", "all_signed")):
                        self._build_and_emit(object_key, obj_config, acl_group,
                                             requester_node_id=None)

            # Signal frontend
            if self._ctx.emit_event:
                self._ctx.emit_event("cache.backend.ready")
            log.info("punchhole-backend: warm start complete — emitted cache.backend.ready")

        except Exception as exc:
            log.error(f"punchhole-backend: startup error: {exc}", exc_info=True)

    # ------------------------------------------------------------------
    # ACL resolution
    # ------------------------------------------------------------------

    def _resolve_acl_group(self, node_id: str) -> str:
        """
        Determine the highest-privilege ACL group for a given node_id.

        Resolution order (most privileged first):
          peer -> known_hosts -> trusted -> all_signed
        """
        if self._is_peer(node_id):
            return "peer"
        if self._is_known_host(node_id):
            return "known_hosts"
        if node_id in self._trusted_nodes:
            return "trusted"
        # all_signed: any node that can present a valid signature passes
        return "all_signed"

    def _is_known_host(self, node_id: str) -> bool:
        """Check address_book WHERE tier = 'explicit'."""
        if not self._ctx.storage_path:
            return False
        try:
            conn = sqlite3.connect(self._ctx.storage_path, timeout=5)
            cur = conn.execute(
                "SELECT 1 FROM address_book WHERE node_id = ? AND tier = 'explicit' LIMIT 1",
                (node_id,),
            )
            result = cur.fetchone() is not None
            conn.close()
            return result
        except Exception as exc:
            log.warning(f"punchhole-backend: address_book query failed: {exc}")
            return False

    def _is_peer(self, node_id: str) -> bool:
        """Check peer_keys table — if the node is known, it's a peer."""
        if not self._ctx.storage_path:
            return False
        try:
            conn = sqlite3.connect(self._ctx.storage_path, timeout=5)
            cur = conn.execute(
                "SELECT 1 FROM peer_keys WHERE node_id = ? LIMIT 1",
                (node_id,),
            )
            result = cur.fetchone() is not None
            conn.close()
            return result
        except Exception as exc:
            log.warning(f"punchhole-backend: peer_keys query failed: {exc}")
            return False

    def _get_all_peer_node_ids(self) -> Set[str]:
        """Return set of all known peer node_ids from peer_keys table."""
        if not self._ctx.storage_path:
            return set()
        try:
            conn = sqlite3.connect(self._ctx.storage_path, timeout=5)
            cur = conn.execute("SELECT node_id FROM peer_keys")
            result = {row[0] for row in cur.fetchall()}
            conn.close()
            return result
        except Exception as exc:
            log.warning(f"punchhole-backend: peer_keys scan failed: {exc}")
            return set()

    def _get_all_known_host_node_ids(self) -> Set[str]:
        """Return set of all node_ids in address_book with tier='explicit'."""
        if not self._ctx.storage_path:
            return set()
        try:
            conn = sqlite3.connect(self._ctx.storage_path, timeout=5)
            cur = conn.execute("SELECT node_id FROM address_book WHERE tier = 'explicit'")
            result = {row[0] for row in cur.fetchall()}
            conn.close()
            return result
        except Exception as exc:
            log.warning(f"punchhole-backend: address_book scan failed: {exc}")
            return set()

    def _check_access(self, requester_node_id: str, required: str) -> bool:
        """
        Check if requester_node_id satisfies the required access entry.

        Handles both tier strings (all_signed, trusted, known_hosts, peer) and
        ACL shorthands:
          group:<name>   — PREREQ-02: member of named group via GroupEngine
          indexer:<name> — PREREQ-03: registered as named indexer in config
        """
        if required.startswith("group:"):
            # PREREQ-02: group membership check via GroupEngine
            group_name = required[len("group:"):]
            ge = getattr(self._ctx, "group_engine", None)
            if ge is None:
                log.warning("punchhole-backend: group_engine not available on ctx")
                return False
            return bool(ge.is_member(requester_node_id, group_name))

        if required.startswith("indexer:"):
            # PREREQ-03: registered indexer check
            indexer_name = required[len("indexer:"):]
            registered_node_id = self._indexers.get(indexer_name, "")
            return bool(registered_node_id and requester_node_id == registered_node_id)

        # Standard tier-based check
        requester_tier = self._resolve_acl_group(requester_node_id)
        return _tier_has_access(requester_tier, required)

    async def _push_acl_config(self):
        """Build ACL map for all known nodes and push to frontend via bus."""
        if not self._ctx.emit_event:
            return

        # Collect all known nodes and their highest-privilege tier
        acl_map: Dict[str, str] = {}

        # C-05: wrap full-table SQLite scan in thread to avoid blocking event loop
        peers = await asyncio.to_thread(self._get_all_peer_node_ids)
        for nid in peers:
            acl_map[nid] = "peer"

        known_hosts = self._get_all_known_host_node_ids()
        for nid in known_hosts:
            if nid not in acl_map:  # peer takes precedence
                acl_map[nid] = "known_hosts"

        for nid in self._trusted_nodes:
            if nid not in acl_map:
                acl_map[nid] = "trusted"

        # Emit per-group batches
        by_group: Dict[str, Dict[str, str]] = {}
        for nid, group in acl_map.items():
            by_group.setdefault(group, {})[nid] = group

        for group, sub_map in by_group.items():
            self._ctx.emit_event(
                f"cache.fill.acl.{group}",
                acl=sub_map,
            )

        if self._debug:
            log.debug(f"punchhole-backend: pushed ACL config, {len(acl_map)} entries")

    # ------------------------------------------------------------------
    # Data readers
    # ------------------------------------------------------------------

    def _read_economy_summary(self) -> Dict[str, Any]:
        """Read aggregated economy data from ledger + skill count."""
        if not self._ctx.storage_path:
            return {}
        try:
            conn = sqlite3.connect(self._ctx.storage_path, timeout=5)
            # Aggregate ledger
            cur = conn.execute("""
                SELECT
                    COALESCE(SUM(balance), 0.0)     AS credit_balance,
                    COALESCE(SUM(tasks_provided)
                           + SUM(tasks_consumed), 0) AS settlement_count
                FROM ledger
            """)
            row = cur.fetchone() or (0.0, 0)
            credit_balance = float(row[0])
            settlement_count = int(row[1])

            # Utilization: simple ratio of positive balances to total soft_limit if available
            utilization = 0.0
            try:
                cur2 = conn.execute("""
                    SELECT COALESCE(SUM(CASE WHEN balance > 0 THEN balance ELSE 0 END), 0.0),
                           COALESCE(SUM(CASE WHEN soft_limit > 0 THEN soft_limit ELSE 1.0 END), 1.0)
                    FROM ledger
                """)
                r2 = cur2.fetchone() or (0.0, 1.0)
                utilization = float(r2[0]) / max(float(r2[1]), 1.0)
            except Exception:
                pass

            # Skill count
            try:
                cur3 = conn.execute("SELECT COUNT(*) FROM skills WHERE is_own = 1")
                skills_count = (cur3.fetchone() or (0,))[0]
            except Exception:
                skills_count = 0

            conn.close()
            return {
                "credit_balance": credit_balance,
                "settlement_count": settlement_count,
                "utilization": utilization,
                "skills_count": skills_count,
            }
        except Exception as exc:
            log.warning(f"punchhole-backend: economy.summary read failed: {exc}")
            return {}

    def _read_economy_bilateral(self, requester_node_id: str) -> Dict[str, Any]:
        """Read bilateral ledger entry for a specific counterparty."""
        if not self._ctx.storage_path or not requester_node_id:
            return {}
        try:
            conn = sqlite3.connect(self._ctx.storage_path, timeout=5)
            cur = conn.execute("""
                SELECT balance,
                       COALESCE(soft_limit, 0.0)  AS soft_limit,
                       COALESCE(prepaid, 0.0)      AS prepaid
                FROM ledger WHERE peer_public_key = ?
            """, (requester_node_id,))
            row = cur.fetchone()
            conn.close()
            if not row:
                return {}
            balance = float(row[0])
            limit = float(row[1])
            prepaid = float(row[2])
            utilization = balance / max(limit, 1.0) if limit > 0 else 0.0
            return {
                "balance": balance,
                "utilization": utilization,
                "limit": limit,
                "prepaid_balance": prepaid,
            }
        except Exception as exc:
            log.warning(f"punchhole-backend: economy.bilateral read failed: {exc}")
            return {}

    def _read_skills(self) -> List[Dict[str, Any]]:
        """Read own published skills."""
        if not self._ctx.storage_path:
            return []
        try:
            conn = sqlite3.connect(self._ctx.storage_path, timeout=5)
            cur = conn.execute("""
                SELECT skill_key, skill_record_json FROM skills WHERE is_own = 1
            """)
            skills = []
            for row in cur.fetchall():
                try:
                    rec = json.loads(row[1])
                    skills.append({
                        "skill_name": row[0],
                        "price": rec.get("price", 0.0),
                        "visibility": rec.get("visibility", "public"),
                    })
                except Exception:
                    pass
            conn.close()
            return skills
        except Exception as exc:
            log.warning(f"punchhole-backend: skills read failed: {exc}")
            return []

    def _read_schemas_skills(self) -> Dict[str, Any]:
        """Read skill schemas from skill registry."""
        if not self._ctx.storage_path:
            return {"skills": []}
        try:
            conn = sqlite3.connect(self._ctx.storage_path, timeout=5)
            cur = conn.execute("""
                SELECT skill_key, skill_record_json FROM skills WHERE is_own = 1
            """)
            skills = []
            for row in cur.fetchall():
                try:
                    rec = json.loads(row[1])
                    skill_data = {
                        "skill_name": row[0],
                        "input_schema": rec.get("input_schema", {}),
                        "output_schema": rec.get("output_schema", {}),
                        "input_spec": rec.get("input_spec", ""),
                    }
                    skills.append(skill_data)
                except Exception:
                    pass
            conn.close()
            return {"skills": skills}
        except Exception as exc:
            log.warning(f"punchhole-backend: schemas.skills read failed: {exc}")
            return {"skills": []}

    def _read_schemas_documents(self) -> Dict[str, Any]:
        """Read document schemas from commerce/documents.py."""
        try:
            from knarr.commerce.documents import _TYPE_REGISTRY, _PREFIX_MAP

            documents = []
            for doc_type, required_fields in _TYPE_REGISTRY.items():
                doc_data = {
                    "document_type": doc_type,
                    "required_fields": list(required_fields),
                    "prefix": _PREFIX_MAP.get(doc_type, "")
                }
                documents.append(doc_data)
            return {"documents": documents}
        except Exception as exc:
            log.warning(f"punchhole-backend: schemas.documents read failed: {exc}")
            return {"documents": []}

    def _read_storefront_catalog(self) -> Dict[str, Any]:
        config_dir = self._ctx.plugin_dir.parents[1]
        expose_path = config_dir / "expose.toml"
        try:
            raw = tomllib.loads(expose_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"catalog": []}
        except Exception as exc:
            log.warning(f"punchhole-backend: storefront.catalog read failed: {exc}")
            return {"catalog": []}

        catalog = []
        for name, cfg in raw.items():
            if not isinstance(cfg, dict):
                continue
            catalog.append({
                "name": name,
                "skill": cfg.get("skill", name),
                "path": cfg.get("path", name),
                "price": cfg.get("payment_amount", cfg.get("price", "")),
                "payment": cfg.get("payment", "none"),
                "payment_asset": cfg.get("payment_asset", ""),
                "payment_network": cfg.get("payment_network", ""),
            })
        catalog.sort(key=lambda item: (str(item.get("path", "")), str(item.get("name", ""))))
        return {"catalog": catalog}

    def _read_economy_full(self) -> Dict[str, Any]:
        """B3: Read full economy data including per-peer ledger details."""
        if not self._ctx.storage_path:
            return {}
        try:
            conn = sqlite3.connect(self._ctx.storage_path, timeout=5)
            cur = conn.execute("""
                SELECT peer_public_key,
                       balance,
                       COALESCE(soft_limit, 0.0) AS soft_limit,
                       COALESCE(prepaid, 0.0) AS prepaid,
                       COALESCE(tasks_provided, 0) AS tasks_provided,
                       COALESCE(tasks_consumed, 0) AS tasks_consumed
                FROM ledger
            """)
            rows = cur.fetchall()
            conn.close()
            peers = []
            for row in rows:
                peers.append({
                    "peer": row[0],
                    "balance": float(row[1]),
                    "soft_limit": float(row[2]),
                    "prepaid": float(row[3]),
                    "tasks_provided": int(row[4]),
                    "tasks_consumed": int(row[5]),
                })
            return {"peers": peers, "count": len(peers)}
        except Exception as exc:
            log.warning(f"punchhole-backend: economy.full read failed: {exc}")
            return {}

    def _read_positions_full(self) -> Dict[str, Any]:
        """B3: Read full position details from ledger."""
        if not self._ctx.storage_path:
            return {}
        try:
            conn = sqlite3.connect(self._ctx.storage_path, timeout=5)
            cur = conn.execute("""
                SELECT peer_public_key,
                       balance,
                       COALESCE(soft_limit, 0.0) AS soft_limit,
                       COALESCE(tasks_provided, 0) AS tasks_provided,
                       COALESCE(tasks_consumed, 0) AS tasks_consumed
                FROM ledger ORDER BY ABS(balance) DESC
            """)
            rows = cur.fetchall()
            conn.close()
            positions = []
            for row in rows:
                balance = float(row[1])
                soft_limit = float(row[2])
                utilization = balance / max(soft_limit, 1.0) if soft_limit != 0 else 0.0
                positions.append({
                    "peer": row[0],
                    "balance": balance,
                    "soft_limit": soft_limit,
                    "utilization": utilization,
                    "tasks_provided": int(row[3]),
                    "tasks_consumed": int(row[4]),
                })
            return {"positions": positions, "count": len(positions)}
        except Exception as exc:
            log.warning(f"punchhole-backend: positions.full read failed: {exc}")
            return {}

    def _read_peers_status(self) -> Dict[str, Any]:
        """B3: Read peer connectivity status."""
        if not self._ctx.storage_path:
            return {}
        try:
            conn = sqlite3.connect(self._ctx.storage_path, timeout=5)
            cur = conn.execute("""
                SELECT node_id, host, port,
                       COALESCE(last_seen, 0) AS last_seen,
                       COALESCE(failed_count, 0) AS failed_count
                FROM peers
                ORDER BY last_seen DESC
            """)
            rows = cur.fetchall()
            conn.close()
            now = time.time()
            peers = []
            for row in rows:
                last_seen = float(row[3])
                age_s = int(now - last_seen) if last_seen > 0 else -1
                peers.append({
                    "node_id": row[0],
                    "host": row[1],
                    "port": int(row[2]),
                    "last_seen_age_s": age_s,
                    "failed_count": int(row[4]),
                })
            return {"peers": peers, "count": len(peers)}
        except Exception as exc:
            log.warning(f"punchhole-backend: peers.status read failed: {exc}")
            return {}

    def _read_skills_inventory(self) -> Dict[str, Any]:
        """B3: Read full skill inventory including all skills."""
        if not self._ctx.storage_path:
            return {}
        try:
            conn = sqlite3.connect(self._ctx.storage_path, timeout=5)
            cur = conn.execute("""
                SELECT skill_key, skill_record_json, is_own
                FROM skills
                ORDER BY is_own DESC, skill_key ASC
            """)
            rows = cur.fetchall()
            conn.close()
            skills = []
            for row in rows:
                try:
                    rec = json.loads(row[1])
                    skills.append({
                        "skill_name": row[0],
                        "price": rec.get("price", 0.0),
                        "visibility": rec.get("visibility", "public"),
                        "is_own": bool(row[2]),
                        "version": rec.get("version", ""),
                        "description": rec.get("description", ""),
                    })
                except Exception:
                    pass
            return {"skills": skills, "count": len(skills)}
        except Exception as exc:
            log.warning(f"punchhole-backend: skills.inventory read failed: {exc}")
            return {}

    # ------------------------------------------------------------------
    # Cache object builder
    # ------------------------------------------------------------------

    def _read_raw_data(self, object_key: str, obj_config: Dict[str, Any],
                       requester_node_id: Optional[str] = None) -> Dict[str, Any]:
        """Dispatch to the correct data reader based on source."""
        source = obj_config.get("source", "")
        if object_key == "economy.summary":
            return self._read_economy_summary()
        if object_key == "economy.bilateral":
            return self._read_economy_bilateral(requester_node_id or "")
        if object_key == "skills":
            return {"skills": self._read_skills()}
        if object_key == "schemas.skills":
            return self._read_schemas_skills()
        if object_key == "schemas.documents":
            return self._read_schemas_documents()
        if object_key == "storefront.catalog":
            return self._read_storefront_catalog()
        if object_key == "economy.full":
            return self._read_economy_full()
        if object_key == "positions.full":
            return self._read_positions_full()
        if object_key == "peers.status":
            return self._read_peers_status()
        if object_key == "skills.inventory":
            return self._read_skills_inventory()
        # Fallback for unknown objects
        return {}

    def _build_cache_object(self, object_key: str, obj_config: Dict[str, Any],
                            acl_group: str,
                            requester_node_id: Optional[str] = None) -> Optional[dict]:
        """
        Build and sign a cache object.

        Returns signed document dict, or None on failure.
        """
        if not self._ctx.sign_document:
            log.error("punchhole-backend: ctx.sign_document not available")
            return None

        raw = self._read_raw_data(object_key, obj_config, requester_node_id)
        if not raw:
            if self._debug:
                log.debug(f"punchhole-backend: no data for {object_key}")
            return None

        fields = obj_config.get("fields", list(raw.keys()))
        granularity = obj_config.get("granularity", {})

        # For bilateral: raw contains flat dict
        if object_key == "skills" and "skills" in raw:
            # Apply list granularity to the skills list
            data = {"skills": _apply_granularity(raw["skills"], "list")}
        elif object_key == "storefront.catalog" and "catalog" in raw:
            data = {"catalog": raw["catalog"]}
        else:
            data = _build_data_dict(raw, fields, granularity)

        doc = {
            "document_type": "cache_object",
            "version": 1,
            "object_key": object_key,
            "acl_group": acl_group,
            "identity": f"did:knarr:{self._ctx.node_id}",
            "counterparty": requester_node_id,
            "built_at": _iso_now(),
            "data": data,
            "granularity": granularity,
            "source_snapshot": {"source": obj_config.get("source", "")},
            "live_query": obj_config.get("live_query", False),
        }

        try:
            signed = self._ctx.sign_document(doc)
            return signed
        except Exception as exc:
            log.error(f"punchhole-backend: signing failed for {object_key}: {exc}")
            return None

    def _build_and_emit(self, object_key: str, obj_config: Dict[str, Any],
                        acl_group: str,
                        requester_node_id: Optional[str] = None) -> None:
        """Build, sign, and emit a cache.fill event."""
        signed = self._build_cache_object(object_key, obj_config, acl_group,
                                          requester_node_id)
        if signed is None:
            return
        self._cache[(object_key, acl_group)] = signed
        if self._ctx.emit_event:
            self._ctx.emit_event(
                f"cache.fill.{object_key}",
                object_key=object_key,
                acl_group=acl_group,
                data=signed,
                requester_node_id=requester_node_id,
            )
            if self._debug:
                log.debug(f"punchhole-backend: emitted cache.fill.{object_key} for {acl_group}")

    def get_private_object(self, key: str) -> Optional[dict]:
        """Read the local private cache directly without going through HTTP."""
        signed = self._cache.get((key, "private"))
        if self._debug:
            log.debug("punchhole-backend: private_read key=%s hit=%s", key, signed is not None)
        return signed

    def get_punchhole_epoch(self) -> int:
        """Return the current cache invalidation epoch for heartbeat bridging."""
        return int(self._punchhole_epoch)

    # ------------------------------------------------------------------
    # Card builder
    # ------------------------------------------------------------------

    def build_card(self, requester_node_id: str) -> Optional[dict]:
        """
        Build a punchhole card for a requester — filtered schema view.

        Card is cached per ACL group. All node_ids in the same group
        receive the same card content (only the for_node field differs).
        """
        if not self._ctx.sign_document:
            return None

        requester_tier = self._resolve_acl_group(requester_node_id)
        available = []
        not_available = []

        for key, obj_config in self._objects.items():
            required = obj_config.get("access", "all_signed")
            if self._check_access(requester_node_id, required):
                available.append({
                    "key": key,
                    "description": obj_config.get("description", ""),
                    "fields": obj_config.get("fields", []),
                    "granularity": obj_config.get("granularity", {}),
                    "live_query": obj_config.get("live_query", False),
                })
            else:
                not_available.append({
                    "key": key,
                    "reason": f"Requires {required}-level access",
                    "upgrade_path": required,
                })

        card = {
            "document_type": "punchhole_card",
            "version": 1,
            "for_node": requester_node_id,
            "for_access_level": requester_tier,
            "available": available,
            "not_available": not_available,
            "built_at": _iso_now(),
            "identity": f"did:knarr:{self._ctx.node_id}",
        }

        try:
            return self._ctx.sign_document(card)
        except Exception as exc:
            log.error(f"punchhole-backend: card signing failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Cache miss loop
    # ------------------------------------------------------------------

    async def _miss_loop(self):
        """Handle cache.miss.* events from frontend."""
        while True:
            try:
                event = await self._miss_sub.next()
                etype = event.get("event", "")
                if not etype.startswith("cache.miss."):
                    continue

                object_key = event.get("object_key", "")
                requester_tier = event.get("requester_tier", "all_signed")
                requester_node_id = event.get("requester_node_id", "")

                if not object_key or object_key not in self._objects:
                    if self._debug:
                        log.debug(f"punchhole-backend: unknown object_key {object_key!r} in miss")
                    continue

                obj_config = self._objects[object_key]
                is_live = obj_config.get("live_query", False)

                if is_live:
                    # Build on demand per requester, use exact requester tier
                    acl_group = self._resolve_acl_group(requester_node_id) \
                        if requester_node_id else requester_tier
                    # Verify peer access for bilateral (supports group:/indexer: shorthands)
                    required = obj_config.get("access", "all_signed")
                    if not self._check_access(requester_node_id or "", required):
                        if self._debug:
                            log.debug(
                                f"punchhole-backend: {requester_node_id} lacks access "
                                f"for {object_key} (needs {required}, has {acl_group})"
                            )
                        continue
                    self._build_and_emit(object_key, obj_config, acl_group, requester_node_id)
                else:
                    # Re-build for requester's tier
                    acl_group = requester_tier or "all_signed"
                    self._build_and_emit(object_key, obj_config, acl_group, None)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error(f"punchhole-backend: miss loop error: {exc}", exc_info=True)
                await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    # Stale watcher loop
    # ------------------------------------------------------------------

    # Dependency map: internal event -> affected object keys
    _STALE_MAP: Dict[str, List[str]] = {
        "credit.change": ["economy.summary", "economy.bilateral", "economy.full", "positions.full"],
        "receipt.issued": ["economy.summary", "economy.full", "positions.full"],
        "skill.registered": ["skills", "schemas.skills", "skills.inventory"],
        "skill.removed": ["skills", "schemas.skills", "skills.inventory"],
        "exposure.changed": ["storefront.catalog"],
        "configuration.order": [],  # handled below — stales card + affected objects
    }

    async def _stale_loop(self):
        """Watch internal events and emit cache.stale.* for affected objects."""
        while True:
            try:
                event = await self._internal_sub.next()
                etype = event.get("event", "")

                stale_keys = self._STALE_MAP.get(etype, [])

                if etype == "configuration.order":
                    # Config changed — stale everything and reload schema
                    # Config orders accepted only from #cockpit-1 (validated by WM Gate 5)
                    stale_keys = list(self._objects.keys())
                    if self._debug:
                        log.debug("punchhole-backend: config order received — staling all objects")

                if stale_keys:
                    # PREREQ-01: increment epoch whenever any cache object is staled
                    self._punchhole_epoch += 1

                for key in stale_keys:
                    if self._ctx.emit_event:
                        self._ctx.emit_event(
                            f"cache.stale.{key}",
                            object_key=key,
                            reason=etype,
                        )
                    if self._debug:
                        log.debug(f"punchhole-backend: emitted cache.stale.{key} (trigger: {etype})")

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error(f"punchhole-backend: stale loop error: {exc}", exc_info=True)
                await asyncio.sleep(0.1)

    async def on_shutdown(self) -> None:
        log.info("punchhole-backend: shutting down")


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    """UTC ISO 8601 timestamp."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
