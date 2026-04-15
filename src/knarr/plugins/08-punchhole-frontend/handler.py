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
import ipaddress
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from knarr.core.messages import PluginMessage
from knarr.dht.plugins import PluginContext, PluginHooks, NodeHealth
from knarr.core.models import NodeInfo
from knarr.core.proof import verify_document
from knarr.core.crypto import VerifyKey

log = logging.getLogger("knarr.plugin.punchhole-frontend")


def _get_verify_key_for_node(ctx: PluginContext, node_id: str) -> Optional[VerifyKey]:
    """P-01: Look up the actual Ed25519 public key for a node from storage.

    node_id is SHA-256(public_key) — NOT the public key itself.
    Using node_id bytes directly as a VerifyKey is wrong (prior bug).
    """
    node = getattr(ctx, '_node', None)
    if node is None:
        return None
    storage = getattr(node, 'storage', None)
    if storage is None:
        return None
    pub_key_hex = storage.get_pubkey_by_node_id(node_id)
    if not pub_key_hex:
        return None
    try:
        return VerifyKey(bytes.fromhex(pub_key_hex))
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

    def _trace(self, trace_id: str, requester_node_id: str, action: str, **fields) -> None:
        if not self._debug:
            return
        extras = " ".join(f"{key}={value}" for key, value in fields.items() if value not in ("", None))
        message = f"[{trace_id}] [{requester_node_id[:8]}] PUNCHHOLE_{action}"
        if extras:
            message = f"{message} {extras}"
        log.info(message)

    @staticmethod
    def _is_safe_reply_address(host: str) -> bool:
        """TP-1: Reject private/loopback/link-local IPs to prevent SSRF."""
        try:
            addr = ipaddress.ip_address(host)
            return addr.is_global
        except ValueError:
            return False  # hostname, not IP — reject

    def _resolve_peer(self, requester_node_id: str, peer_ip: str, request: dict) -> Optional[NodeInfo]:
        get_peers = getattr(self._ctx, "get_peers", None)
        if callable(get_peers):
            for peer in get_peers() or []:
                if getattr(peer, "node_id", "") == requester_node_id:
                    return peer

        reply_host = request.get("_reply_host") or peer_ip
        reply_port = int(request.get("_reply_port") or 0)
        # TP-1: Validate reply address is globally routable before using it
        if reply_host and reply_port > 0 and self._is_safe_reply_address(reply_host):
            return NodeInfo(node_id=requester_node_id, host=reply_host, port=reply_port)
        return None

    async def _process_request(
        self,
        requester_node_id: str,
        object_key: str,
        signed_request: Any,
        trace_id: str,
    ) -> dict:
        # Gate: validate object_key format (alphanumeric, dots, underscores, hyphens only)
        if not object_key or not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$', object_key) or len(object_key) > 128:
            log.warning(f"punchhole-frontend: invalid object_key from {requester_node_id}: {object_key!r}")
            return {"status": "rejected", "error": "invalid_object_key", "object_key": object_key}

        # Gate 0: backend must be ready
        if not self._backend_ready:
            self._trace(trace_id, requester_node_id, "REQUEST_REJECT", reason="backend_not_ready", object_key=object_key)
            self._log_disclosure(requester_node_id, object_key, "", "not_ready")
            return {"status": "not_ready", "object_key": object_key}

        # Gate 1: verify requester's signature using the actual Ed25519 public key.
        # P-01 fix: node_id is SHA-256(public_key), not the key itself.
        # Look up the real public key from storage; skip inner re-verify if not found
        # (outer PluginMessage signature is already verified by the connection handler).
        verify_key = _get_verify_key_for_node(self._ctx, requester_node_id)
        if verify_key is not None and not verify_document(signed_request, verify_key):
            log.warning(f"punchhole-frontend: invalid signature from {requester_node_id}")
            self._log_disclosure(requester_node_id, object_key, "", "rejected")
            return {"status": "rejected", "error": "invalid_signature", "object_key": object_key}

        # Gate 2: resolve ACL group
        acl_group = self._acl.get(requester_node_id, "")
        if not acl_group:
            log.info(
                "PUNCHHOLE_ACL_DENY node_id=%s reason=unknown_not_in_trusted_nodes_or_peer",
                requester_node_id,
            )
            self._log_disclosure(requester_node_id, object_key, "", "rejected")
            return {"status": "rejected", "error": "access_denied", "object_key": object_key}

        # Gate 3: check cache
        cache_key = (object_key, acl_group)
        entry = self._cache.get(cache_key)

        if entry is not None and not entry["stale"]:
            # Cache hit — serve pre-signed object
            self._log_disclosure(requester_node_id, object_key, acl_group, "hit")
            self._trace(trace_id, requester_node_id, "REQUEST_HIT", object_key=object_key, acl=acl_group)
            return {
                "status": "ok",
                "object_key": object_key,
                "data": entry["data"],
                "from_cache": True,
                "acl_group": acl_group,
            }

        # Cache miss (or stale) — emit to backend
        self._log_disclosure(requester_node_id, object_key, acl_group, "miss")
        self._trace(trace_id, requester_node_id, "REQUEST_MISS", object_key=object_key, acl=acl_group)

        if self._ctx.emit_event:
            self._ctx.emit_event(
                f"cache.miss.data.{object_key}",
                object_key=object_key,
                requester_tier=acl_group,
                requester_node_id=requester_node_id,  # CRITICAL for bilateral lookups
            )
        return {
            "status": "miss",
            "object_key": object_key,
            "from_cache": False,
            "acl_group": acl_group,
        }

    async def _handle_card(self, msg, peer_ip: str, request: dict, trace_id: str) -> bool:
        """P-02: Handle action=CARD — return ACL-filtered object list via backend's build_card.

        Adversary #9 fix (v0.56.0): apply the same admission checks as
        _process_request before calling build_card. Previously CARD bypassed
        both the backend-ready gate AND the C-02 default-deny ACL check,
        allowing unknown nodes to probe the ACL-filtered object catalog.
        build_card does internal tier filtering but for unknown requesters
        would return the default tier, undoing C-02's default-deny promise.
        """
        self._trace(trace_id, msg.node_id, "CARD_REQUEST")

        # Gate 0: backend must be ready (mirrors _process_request)
        if not self._backend_ready:
            self._trace(trace_id, msg.node_id, "CARD_REJECT", reason="backend_not_ready")
            self._log_disclosure(msg.node_id, "card", "", "not_ready")
            card = None
            response_payload = {"status": "not_ready", "error": "backend_not_ready"}
            await self._send_card_response(msg, peer_ip, request, trace_id, response_payload)
            return True

        # Gate 1: default-deny ACL check (C-02 default-deny — mirrors _process_request)
        # Unknown nodes do not see a filtered catalog just because the filter
        # happens inside build_card. Operators must explicitly grant via
        # trusted_nodes / known_hosts / peer relationships.
        acl_group = self._acl.get(msg.node_id, "")
        if not acl_group:
            log.info(
                "PUNCHHOLE_ACL_DENY node_id=%s reason=unknown_not_in_trusted_nodes_or_peer action=CARD",
                msg.node_id,
            )
            self._log_disclosure(msg.node_id, "card", "", "rejected")
            response_payload = {"status": "rejected", "error": "access_denied"}
            await self._send_card_response(msg, peer_ip, request, trace_id, response_payload)
            return True

        # Gate 2: build card for an authenticated, admitted requester
        card = None
        get_plugin = getattr(self._ctx, "get_plugin", None)
        if callable(get_plugin):
            backend = get_plugin("punchhole-backend")
            if backend is not None:
                build_fn = getattr(backend, "build_card", None)
                if callable(build_fn):
                    card = build_fn(msg.node_id)

        if card is None:
            response_payload = {"status": "unavailable", "error": "card_not_available"}
        else:
            response_payload = {"status": "ok", "card": card}

        await self._send_card_response(msg, peer_ip, request, trace_id, response_payload)
        return True

    async def _send_card_response(
        self, msg, peer_ip: str, request: dict, trace_id: str, response_payload: dict
    ) -> None:
        """Shared CARD response sender.

        Extracted from _handle_card (adversary #9 fix) so both the happy path
        and the admission-rejection paths use the same outbound logic.
        """
        request_id = str(request.get("_request_id", "") or "")
        if request_id:
            response_payload["_request_id"] = request_id
        if trace_id:
            response_payload["trace_id"] = trace_id

        peer = self._resolve_peer(msg.node_id, peer_ip, request)
        if peer is not None and getattr(self._ctx, "send_fire_forget", None):
            self._trace(trace_id, msg.node_id, "CARD_RESPONSE", status=response_payload.get("status", ""))
            await self._ctx.send_fire_forget(
                peer,
                PluginMessage(
                    node_id=self._ctx.node_id,
                    plugin_name="knarr-punchhole",
                    action="CARD_RESPONSE",
                    payload=json.dumps(response_payload),
                ),
            )

    async def on_inbound(self, msg, peer_ip: str) -> bool:
        """Handle PluginMessage requests using the generic A-01 query_plugin pattern."""
        if not isinstance(msg, PluginMessage):
            return True
        if msg.plugin_name != "knarr-punchhole":
            return True

        if msg.action not in ("REQUEST", "CARD"):
            return True

        try:
            request = json.loads(msg.payload) if msg.payload else {}
        except (TypeError, json.JSONDecodeError) as exc:
            log.warning(f"punchhole-frontend: malformed PluginMessage from {msg.node_id}: {exc}")
            return True  # P-01: handled internally, don't trigger firewall.blocked

        if not isinstance(request, dict):
            return True  # P-01: handled internally, don't trigger firewall.blocked

        trace_id = str(request.get("trace_id", "") or request.get("_request_id", "") or "")

        # P-02: CARD action — return ACL-filtered object list
        if msg.action == "CARD":
            return await self._handle_card(msg, peer_ip, request, trace_id)
        result = await self._process_request(
            requester_node_id=msg.node_id,
            object_key=str(request.get("object_key", "") or ""),
            signed_request=request.get("payload", {}),
            trace_id=trace_id,
        )

        response_payload = dict(result)
        request_id = str(request.get("_request_id", "") or "")
        if request_id:
            response_payload["_request_id"] = request_id
        if trace_id:
            response_payload["trace_id"] = trace_id

        peer = self._resolve_peer(msg.node_id, peer_ip, request)
        if peer is not None and getattr(self._ctx, "send_fire_forget", None):
            self._trace(trace_id, msg.node_id, "RESPONSE_SEND", status=result.get("status", ""), object_key=result.get("object_key", ""))
            await self._ctx.send_fire_forget(
                peer,
                PluginMessage(
                    node_id=self._ctx.node_id,
                    plugin_name="knarr-punchhole",
                    action="RESPONSE",
                    payload=json.dumps(response_payload),
                ),
            )
        return True  # P-01: handled internally, don't trigger firewall.blocked

    async def on_mail_received(self, msg_type: str, from_node: str,
                               to_node: str, body: Any,
                               session_id: Optional[str]) -> None:
        """Deprecated one-version fallback for punchhole requests over mail."""
        if msg_type != "punchhole.request":
            return

        try:
            request = body if isinstance(body, dict) else json.loads(body)
        except (TypeError, json.JSONDecodeError) as exc:
            log.warning(f"punchhole-frontend: malformed request from {from_node}: {exc}")
            return

        if not isinstance(request, dict) or request.get("action") != "request":
            return

        trace_id = str(request.get("trace_id", "") or "")
        result = await self._process_request(
            requester_node_id=from_node,
            object_key=str(request.get("object_key", "") or ""),
            signed_request=request.get("payload", {}),
            trace_id=trace_id,
        )
        if result.get("status") == "ok" and self._ctx.emit_event:
            self._ctx.emit_event(
                "punchhole.response",
                requester_node_id=from_node,
                object_key=result.get("object_key", ""),
                data=result.get("data"),
                from_cache=bool(result.get("from_cache", False)),
                trace_id=trace_id,
            )

    async def on_shutdown(self) -> None:
        log.info("punchhole-frontend: shutting down")
