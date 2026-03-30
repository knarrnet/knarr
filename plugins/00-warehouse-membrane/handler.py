from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
from typing import Any, Optional

from knarr.core.messages import Envelope, Message, PluginMessage
from knarr.core.uri import parse_knarr_uri
from knarr.dht.node import _current_identity
from knarr.dht.plugins import PluginContext, PluginHooks

log = logging.getLogger("knarr.plugin.warehouse-membrane")

_DEFAULT_SELECTOR_PLUGINS = {
    "s": "__skills__",
    "c": "__commerce__",
    "p": "knarr-punchhole",
    "m": "__mail__",
    "k": "knarr-kademlia",
    "g": "knarr-groups",
    "o": "__objects__",
}


class WarehouseMembranePlugin(PluginHooks):
    """Validate URI-addressed inbound traffic and scope the active identity."""

    def __init__(self, ctx: PluginContext, config: dict):
        self._ctx = ctx
        self._config = dict(config or {})
        self._debug = bool(self._config.get("debug", False) or getattr(ctx, "_debug", False))
        self._selector_plugins = dict(_DEFAULT_SELECTOR_PLUGINS)
        self._selector_plugins.update(
            {str(key): str(value) for key, value in (self._config.get("selector_plugins", {}) or {}).items()}
        )
        self._rules = {
            str(pattern): dict(rule)
            for pattern, rule in (self._config.get("rules", {}) or {}).items()
            if isinstance(rule, dict)
        }

    def _log_gate(self, trace_id: str, identity: Any, action: str, **fields) -> None:
        if not self._debug:
            return
        identity_prefix = str(getattr(identity, "node_id", "") or "")[:8]
        extras = " ".join(f"{key}={value}" for key, value in fields.items() if value not in ("", None))
        message = f"[{trace_id}] [{identity_prefix}] WM_{action}"
        if extras:
            message = f"{message} {extras}"
        log.info(message)

    def _match_rule(self, uri: str) -> Optional[dict]:
        for pattern, rule in self._rules.items():
            if fnmatch.fnmatch(uri, pattern):
                return rule
        return None

    def _payload_dict(self, msg: Message) -> Optional[dict]:
        if isinstance(msg, Envelope):
            if not msg.payload:
                return {}
            try:
                payload = json.loads(msg.payload)
            except (TypeError, json.JSONDecodeError):
                return None
            return payload if isinstance(payload, dict) else None

        if isinstance(msg, PluginMessage):
            try:
                payload = json.loads(msg.payload) if msg.payload else {}
            except (TypeError, json.JSONDecodeError):
                return None
            return payload if isinstance(payload, dict) else None

        return {}

    def _message_uri(self, msg: Message, payload: Optional[dict]) -> str:
        if isinstance(msg, Envelope):
            return str(msg.uri or "")
        if isinstance(msg, PluginMessage):
            if payload is None and msg.payload:
                return "__INVALID__"
            return str((payload or {}).get("uri", "") or "")
        return ""

    def _resolve_identity(self, authority: str):
        node = getattr(self._ctx, "_node", None)
        registry = getattr(node, "_identity_registry", None)
        if registry is None:
            return None
        if authority:
            return registry.resolve(authority)
        return registry.default

    def _selector_is_registered(self, selector: str) -> bool:
        target = self._selector_plugins.get(selector, "")
        if not target:
            return False
        if target.startswith("__"):
            return True
        getter = getattr(self._ctx, "get_plugin", None)
        return callable(getter) and getter(target) is not None

    def _authorize_caller(self, msg: Message) -> bool:
        """Gate 5: Verify caller has a valid identity (public key or node_id).

        # NOTE: This gate only verifies caller has a valid identity.
        # Full ACL-based authorization deferred to future sprint.
        """
        public_key = str(getattr(msg, "public_key", "") or "")
        if public_key:
            try:
                caller_id = hashlib.sha256(bytes.fromhex(public_key)).hexdigest()
            except ValueError:
                return False
            return bool(caller_id)
        return bool(getattr(msg, "node_id", "") or getattr(msg, "sender_node_id", ""))

    async def _run_commerce_ingest(self, msg: Message, payload: Optional[dict], trace_id: str, identity) -> bool:
        node = getattr(self._ctx, "_node", None)
        ingest = getattr(node, "_wm_ingest", None)
        if not callable(ingest):
            return True

        document = (payload or {}).get("document", payload)
        if not isinstance(document, dict):
            self._log_gate(trace_id, identity, "GATE_FAIL", gate="commerce", reason="invalid_document")
            return False

        originator_pubkey = b""
        public_key = str(getattr(msg, "public_key", "") or "")
        if public_key:
            try:
                originator_pubkey = bytes.fromhex(public_key)
            except ValueError:
                self._log_gate(trace_id, identity, "GATE_FAIL", gate="commerce", reason="invalid_public_key")
                return False

        result = await ingest(document, originator_pubkey)
        if result is None:
            return True
        if getattr(result, "status", "") != "promoted":
            self._log_gate(
                trace_id,
                identity,
                "GATE_FAIL",
                gate="commerce",
                status=getattr(result, "status", ""),
                reason=getattr(result, "reason", ""),
            )
            return False
        return True

    async def on_inbound(self, msg: Message, peer_ip: str) -> bool:
        payload = self._payload_dict(msg)
        trace_id = str(getattr(msg, "trace_id", "") or (payload or {}).get("trace_id", "") or "")
        uri = self._message_uri(msg, payload)

        if not uri:
            self._log_gate(trace_id, None, "LEGACY_BYPASS", msg_type=msg.type)
            return True

        authority, selector, resource = parse_knarr_uri(uri)
        if not selector:
            self._log_gate(trace_id, None, "GATE_FAIL", gate=1, reason="invalid_uri", peer_ip=peer_ip or "")
            return False

        rule = self._match_rule(uri)
        if rule is None:
            self._log_gate(trace_id, None, "GATE_FAIL", gate="rule", reason="no_matching_rule", selector=selector)
            return False

        gates = {int(gate) for gate in rule.get("gates", [])}
        # TP-9: Only resolve identity when gate 2 is active — remove empty authority bypass
        identity = self._resolve_identity(authority) if 2 in gates else None

        if 2 in gates and identity is None:
            self._log_gate(trace_id, None, "GATE_FAIL", gate=2, reason="unknown_identity", authority=authority)
            return False

        if 3 in gates and not self._selector_is_registered(selector):
            self._log_gate(trace_id, identity, "GATE_FAIL", gate=3, reason="unregistered_selector", selector=selector)
            return False

        if 4 in gates and payload is None:
            self._log_gate(trace_id, identity, "GATE_FAIL", gate=4, reason="invalid_payload", selector=selector)
            return False

        if 5 in gates and not self._authorize_caller(msg):
            # TP-2: Gate 5 checks identity, not full authorization
            self._log_gate(trace_id, identity, "GATE_FAIL", gate=5, reason="unidentified", selector=selector)
            return False

        # TP-4: Set identity context BEFORE commerce ingest so ingest runs with correct scope
        if identity is not None:
            _current_identity.set(identity)

        if selector == "c" and not await self._run_commerce_ingest(msg, payload, trace_id, identity):
            return False

        self._log_gate(
            trace_id,
            identity,
            "GATE_PASS",
            selector=selector,
            resource=resource,
            route_action=rule.get("action", ""),
        )
        return True
