"""
Punchhole Cache Frontend Plugin — DMZ side.

Serves pre-signed cache objects to authenticated external requesters.
All configuration and ACL data pushed from backend via bus.
All signing performed by backend — never here.

Airgap invariants (enforced by design):
  - No ctx.sign_document usage.
  - No config file reads.
  - No direct DB or backend plugin access.
  - Requests rejected until cache.backend.ready received.

Bus subscriptions:
  cache.fill.*       — receive signed cache objects from backend
  cache.stale.*      — mark cached entries stale (triggers miss on next request)
  cache.backend.ready — unlock request handling

Bus emissions:
  cache.miss.data.{object_key} — on cache miss, includes requester_node_id
"""

import asyncio
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from knarr.dht.plugins import PluginContext, PluginHooks, NodeHealth
from knarr.core.models import NodeInfo
from knarr.core.proof import verify_document
from nacl.signing import VerifyKey
from nacl.encoding import HexEncoder

log = logging.getLogger("knarr.plugin.punchhole-frontend")


def _hex_to_verify_key(node_id_hex: str) -> Optional[VerifyKey]:
    """Convert a 64-char hex node_id to a VerifyKey, or None on failure."""
    try:
        raw = bytes.fromhex(node_id_hex)
        if len(raw) != 32:
            return None
        return VerifyKey(raw)
    except Exception:
        return None


class PunchholeFrontendPlugin(PluginHooks):
    """
    Punchhole Frontend — serves pre-signed disclosure cache to authenticated requesters.

    Cache store: dict keyed by (object_key, acl_group).
    Entries have shape: {"data": signed_obj, "stale": bool}.
    ACL map: dict[node_id -> acl_group] pushed from backend.
    """

    def __init__(self, ctx: PluginContext, config: dict):
        self._ctx = ctx
        self._config = config
        self._debug = config.get("debug", False)

        # Cache store: (object_key, acl_group) -> {"data": signed_obj, "stale": bool}
        self._cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

        # ACL map: node_id -> acl_group (pushed from backend via cache.fill.acl.*)
        self._acl: Dict[str, str] = {}

        # Guard: do not serve requests until backend signals ready
        self._backend_ready: bool = False

        # Disclosure log DB (plugin-local SQLite)
        db_name = config.get("disclosure_log", "disclosure.db")
        state_dir = ctx.state_dir or ctx.plugin_dir
        self._db_path = str(state_dir / db_name)
        self._init_disclosure_log()

        # Subscribe to bus events
        if ctx.subscribe_events:
            self._sub = ctx.subscribe_events(
                "cache.fill.*",
                "cache.stale.*",
                "cache.backend.ready",
            )
            asyncio.ensure_future(self._bus_loop())
        else:
            log.warning("punchhole-frontend: subscribe_events not available — bus integration disabled")
            self._sub = None

        log.info("punchhole-frontend: initialized (waiting for cache.backend.ready)")

    # ------------------------------------------------------------------
    # Disclosure log
    # ------------------------------------------------------------------

    def _init_disclosure_log(self):
        """Create disclosure_log table in plugin-local SQLite."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS disclosure_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                requester   TEXT NOT NULL,
                object_key  TEXT NOT NULL,
                acl_group   TEXT NOT NULL,
                outcome     TEXT NOT NULL,  -- hit / miss / rejected / not_ready
                ts          REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dl_requester ON disclosure_log(requester)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dl_ts ON disclosure_log(ts)")
        conn.commit()
        conn.close()

    def _log_disclosure(self, requester: str, object_key: str,
                        acl_group: str, outcome: str) -> None:
        """Append one row to the disclosure log. Non-fatal on error."""
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "INSERT INTO disclosure_log (requester, object_key, acl_group, outcome, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (requester, object_key, acl_group, outcome, time.time()),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            log.warning(f"punchhole-frontend: disclosure log write failed: {exc}")

    # ------------------------------------------------------------------
    # Bus loop
    # ------------------------------------------------------------------

    async def _bus_loop(self):
        """Process bus events from backend asynchronously."""
        while True:
            try:
                event = await self._sub.next()
                etype = event.get("event", "")

                if etype == "cache.backend.ready":
                    self._backend_ready = True
                    if self._debug:
                        log.debug("punchhole-frontend: backend ready — accepting requests")

                elif etype.startswith("cache.fill.acl."):
                    # ACL push: event fields: node_id -> acl_group mapping (dict)
                    acl_data = event.get("acl", {})
                    if isinstance(acl_data, dict):
                        self._acl.update(acl_data)
                        if self._debug:
                            log.debug(f"punchhole-frontend: ACL updated, {len(acl_data)} entries")

                elif etype.startswith("cache.fill."):
                    # Signed cache object from backend
                    object_key = event.get("object_key", "")
                    acl_group = event.get("acl_group", "")
                    signed_obj = event.get("data")
                    if object_key and acl_group and signed_obj is not None:
                        self._cache[(object_key, acl_group)] = {"data": signed_obj, "stale": False}
                        if self._debug:
                            log.debug(f"punchhole-frontend: cached ({object_key}, {acl_group})")

                elif etype.startswith("cache.stale."):
                    # Mark entries stale by object_key
                    object_key = event.get("object_key", "")
                    if object_key:
                        count = 0
                        for key in list(self._cache.keys()):
                            if key[0] == object_key:
                                self._cache[key]["stale"] = True
                                count += 1
                        if self._debug:
                            log.debug(f"punchhole-frontend: marked {count} entries stale for {object_key!r}")

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error(f"punchhole-frontend: bus loop error: {exc}", exc_info=True)
                await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    # Request handler
    # ------------------------------------------------------------------

    async def on_mail_received(self, msg_type: str, from_node: str,
                               to_node: str, body: Any,
                               session_id: Optional[str]) -> None:
        """
        Handle punchhole requests arriving as mail.

        Expected body shape:
          {
            "action": "request",
            "object_key": "economy.summary",
            "payload": { ...signed_request... }
          }
        """
        if msg_type != "punchhole.request":
            return

        try:
            request = body if isinstance(body, dict) else json.loads(body)
        except (TypeError, json.JSONDecodeError) as exc:
            log.warning(f"punchhole-frontend: malformed request from {from_node}: {exc}")
            return

        if not isinstance(request, dict):
            log.warning(f"punchhole-frontend: expected dict body from {from_node}, got {type(request).__name__}")
            return

        action = request.get("action")
        if action != "request":
            return

        object_key = request.get("object_key", "")
        signed_request = request.get("payload", {})
        requester_node_id = from_node

        # Gate: validate object_key format (alphanumeric, dots, underscores, hyphens only)
        if not object_key or not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$', object_key) or len(object_key) > 128:
            log.warning(f"punchhole-frontend: invalid object_key from {requester_node_id}: {object_key!r}")
            return

        # Gate 0: backend must be ready
        if not self._backend_ready:
            if self._debug:
                log.debug(f"punchhole-frontend: rejecting request from {requester_node_id} — backend not ready")
            self._log_disclosure(requester_node_id, object_key, "", "not_ready")
            return

        # Gate 1: verify requester's signature
        verify_key = _hex_to_verify_key(requester_node_id)
        if verify_key is None or not verify_document(signed_request, verify_key):
            log.warning(f"punchhole-frontend: invalid signature from {requester_node_id}")
            self._log_disclosure(requester_node_id, object_key, "", "rejected")
            return

        # Gate 2: resolve ACL group
        acl_group = self._acl.get(requester_node_id, "")
        if not acl_group:
            # Unknown node — default to "all_signed" tier; backend handles full resolution
            acl_group = "all_signed"

        # Gate 3: check cache
        cache_key = (object_key, acl_group)
        entry = self._cache.get(cache_key)

        if entry is not None and not entry["stale"]:
            # Cache hit — serve pre-signed object
            self._log_disclosure(requester_node_id, object_key, acl_group, "hit")
            if self._debug:
                log.debug(f"punchhole-frontend: cache hit ({object_key}, {acl_group}) for {requester_node_id}")
            # Response handled by caller reading the return value;
            # for now emit a delivery event so the node can route the reply.
            if self._ctx.emit_event:
                self._ctx.emit_event(
                    "punchhole.response",
                    requester_node_id=requester_node_id,
                    object_key=object_key,
                    data=entry["data"],
                    from_cache=True,
                )
            return

        # Cache miss (or stale) — emit to backend
        self._log_disclosure(requester_node_id, object_key, acl_group, "miss")
        if self._debug:
            log.debug(f"punchhole-frontend: cache miss ({object_key}, {acl_group}) for {requester_node_id}")

        if self._ctx.emit_event:
            self._ctx.emit_event(
                f"cache.miss.data.{object_key}",
                object_key=object_key,
                requester_tier=acl_group,
                requester_node_id=requester_node_id,  # CRITICAL for bilateral lookups
            )

    async def on_shutdown(self) -> None:
        log.info("punchhole-frontend: shutting down")
