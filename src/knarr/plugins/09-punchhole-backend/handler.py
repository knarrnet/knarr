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
_TIER_ORDER = ["all_signed", "trusted", "known_hosts", "peer"]


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

        # Load schema
        schema_file = config.get("schema_file", "exposure_schema.toml")
        schema_path = ctx.plugin_dir / schema_file
        schema_data = _load_schema(schema_path)
        self._objects: Dict[str, Any] = schema_data["objects"]
        self._trusted_nodes: List[str] = schema_data["trusted_nodes"]
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
            # Resolve ACL for all tiers and push to frontend
            self._push_acl_config()

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
        """Check bilateral ledger for entries with this node."""
        if not self._ctx.storage_path:
            return False
        try:
            conn = sqlite3.connect(self._ctx.storage_path, timeout=5)
            # node_id in ledger is stored as peer_public_key (hex)
            cur = conn.execute(
                "SELECT 1 FROM ledger WHERE peer_public_key = ? LIMIT 1",
                (node_id,),
            )
            result = cur.fetchone() is not None
            conn.close()
            return result
        except Exception as exc:
            log.warning(f"punchhole-backend: ledger query failed: {exc}")
            return False

    def _get_all_peer_node_ids(self) -> Set[str]:
        """Return set of all node_ids with ledger entries."""
        if not self._ctx.storage_path:
            return set()
        try:
            conn = sqlite3.connect(self._ctx.storage_path, timeout=5)
            cur = conn.execute("SELECT peer_public_key FROM ledger")
            result = {row[0] for row in cur.fetchall()}
            conn.close()
            return result
        except Exception as exc:
            log.warning(f"punchhole-backend: ledger scan failed: {exc}")
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

    def _push_acl_config(self):
        """Build ACL map for all known nodes and push to frontend via bus."""
        if not self._ctx.emit_event:
            return

        # Collect all known nodes and their highest-privilege tier
        acl_map: Dict[str, str] = {}

        peers = self._get_all_peer_node_ids()
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
            if _tier_has_access(requester_tier, required):
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
                    # Verify peer access for bilateral
                    required = obj_config.get("access", "all_signed")
                    if not _tier_has_access(acl_group, required):
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
        "credit.change": ["economy.summary", "economy.bilateral"],
        "receipt.issued": ["economy.summary"],
        "skill.registered": ["skills"],
        "skill.removed": ["skills"],
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
