"""Tests for v0.55.0 P-01/P-02 — punchhole signature fix and CARD query."""

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest

from knarr.core.crypto import SigningKey, VerifyKey
from knarr.core.proof import sign_document, verify_document

# Load punchhole-frontend handler (directory name has hyphens, can't normal-import)
_HANDLER_PATH = Path(__file__).parent.parent.parent / "src" / "knarr" / "plugins" / "08-punchhole-frontend" / "handler.py"
_spec = importlib.util.spec_from_file_location("punchhole_frontend_handler", _HANDLER_PATH)
_ph_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ph_module)
_get_verify_key_for_node = _ph_module._get_verify_key_for_node
PunchholeFrontendPlugin = _ph_module.PunchholeFrontendPlugin


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_signing_key():
    return SigningKey.generate()


def make_node_id(sk: SigningKey) -> str:
    return hashlib.sha256(sk.verify_key.encode()).hexdigest()


def make_ctx(storage=None, node_id=None):
    """Build a minimal PluginContext-like mock."""
    ctx = MagicMock()
    ctx.node_id = node_id or "a" * 64
    ctx.state_dir = Path(tempfile.mkdtemp())
    ctx.plugin_dir = ctx.state_dir
    ctx.subscribe_events = None
    ctx.emit_event = None
    ctx.send_fire_forget = None
    ctx.get_plugin = None

    # Wire _node.storage
    mock_node = MagicMock()
    mock_node.storage = storage or MagicMock()
    ctx._node = mock_node

    return ctx


# ── P-01: _get_verify_key_for_node ────────────────────────────────────────────

class TestGetVerifyKeyForNode:
    def test_returns_verify_key_when_pubkey_found(self):
        sk = make_signing_key()
        node_id = make_node_id(sk)
        pub_key_hex = sk.verify_key.encode().hex()

        storage = MagicMock()
        storage.get_pubkey_by_node_id.return_value = pub_key_hex
        ctx = make_ctx(storage=storage)

        vk = _get_verify_key_for_node(ctx, node_id)
        assert vk is not None
        assert isinstance(vk, VerifyKey)
        storage.get_pubkey_by_node_id.assert_called_once_with(node_id)

    def test_returns_none_when_pubkey_not_found(self):
        
        storage = MagicMock()
        storage.get_pubkey_by_node_id.return_value = None
        ctx = make_ctx(storage=storage)

        result = _get_verify_key_for_node(ctx, "a" * 64)
        assert result is None

    def test_returns_none_when_no_node(self):
        
        ctx = MagicMock()
        del ctx._node  # no _node attribute
        ctx._node = None

        result = _get_verify_key_for_node(ctx, "a" * 64)
        assert result is None

    def test_verify_key_validates_real_signature(self):
        """The returned VerifyKey can actually verify a document signed by the key."""
        
        sk = make_signing_key()
        node_id = make_node_id(sk)
        pub_key_hex = sk.verify_key.encode().hex()

        storage = MagicMock()
        storage.get_pubkey_by_node_id.return_value = pub_key_hex
        ctx = make_ctx(storage=storage)

        vk = _get_verify_key_for_node(ctx, node_id)
        assert vk is not None

        # Sign a document and verify with the returned key
        doc = {"data": "test", "node_id": node_id}
        signed = sign_document(doc, sk, f"did:knarr:{node_id}#key-1")
        assert verify_document(signed, vk) is True

    def test_old_hex_to_verify_key_was_wrong(self):
        """Prove the old approach (node_id bytes → VerifyKey) is incorrect."""
        sk = make_signing_key()
        node_id = make_node_id(sk)
        pub_key_hex = sk.verify_key.encode().hex()

        # OLD (wrong): node_id bytes directly as VerifyKey
        old_vk = VerifyKey(bytes.fromhex(node_id))

        # NEW (correct): actual public key
        new_vk = VerifyKey(bytes.fromhex(pub_key_hex))

        # They should be different (unless collision — extremely unlikely)
        assert old_vk.encode() != new_vk.encode()

        # Only the new key can verify signatures
        doc = {"data": "test"}
        signed = sign_document(doc, sk, f"did:knarr:{node_id}#key-1")

        assert verify_document(signed, new_vk) is True
        assert verify_document(signed, old_vk) is False  # old approach was wrong


# ── P-01: _process_request gate behavior ──────────────────────────────────────

class TestProcessRequestGate1:
    def _make_plugin(self, storage=None):
        
        ctx = make_ctx(storage=storage)
        plugin = PunchholeFrontendPlugin.__new__(PunchholeFrontendPlugin)
        plugin._ctx = ctx
        plugin._config = {}
        plugin._debug = False
        plugin._cache = {}
        plugin._acl = {}
        plugin._backend_ready = True
        # Minimal disclosure log setup
        import sqlite3
        plugin._db_path = str(ctx.state_dir / "disclosure.db")
        conn = sqlite3.connect(plugin._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS disclosure_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requester TEXT, object_key TEXT, acl_group TEXT, outcome TEXT, ts REAL
            )
        """)
        conn.commit()
        conn.close()
        return plugin

    @pytest.mark.asyncio
    async def test_gate1_passes_when_verify_key_not_found(self):
        """If no public key in storage, Gate 1 is skipped (outer PluginMessage already verified)."""
        storage = MagicMock()
        storage.get_pubkey_by_node_id.return_value = None
        plugin = self._make_plugin(storage=storage)

        # Backend ready, no cache entry → should get "miss" not "rejected"
        result = await plugin._process_request(
            requester_node_id="a" * 64,
            object_key="test.key",
            signed_request={},
            trace_id="t1",
        )
        assert result["status"] == "miss"  # not "rejected" — Gate 1 bypassed

    @pytest.mark.asyncio
    async def test_gate1_rejects_invalid_signature_when_key_found(self):
        """If public key found but signature invalid, Gate 1 rejects."""
        sk = make_signing_key()
        node_id = make_node_id(sk)
        pub_key_hex = sk.verify_key.encode().hex()

        storage = MagicMock()
        storage.get_pubkey_by_node_id.return_value = pub_key_hex
        plugin = self._make_plugin(storage=storage)

        # Pass a tampered/empty signed_request
        result = await plugin._process_request(
            requester_node_id=node_id,
            object_key="test.key",
            signed_request={"proof": {"proofValue": "z" + "a" * 87}},  # invalid
            trace_id="t2",
        )
        assert result["status"] == "rejected"
        assert result["error"] == "invalid_signature"

    @pytest.mark.asyncio
    async def test_gate1_passes_valid_signature(self):
        """Valid signed request passes Gate 1 and continues to cache miss."""
        sk = make_signing_key()
        node_id = make_node_id(sk)
        pub_key_hex = sk.verify_key.encode().hex()

        storage = MagicMock()
        storage.get_pubkey_by_node_id.return_value = pub_key_hex
        plugin = self._make_plugin(storage=storage)
        plugin._ctx.emit_event = None  # no bus

        doc = {"request": "cache_object", "object_key": "data.key"}
        signed = sign_document(doc, sk, f"did:knarr:{node_id}#key-1")

        result = await plugin._process_request(
            requester_node_id=node_id,
            object_key="data.key",
            signed_request=signed,
            trace_id="t3",
        )
        # Gate 1 passes — result is miss (no cache entry)
        assert result["status"] == "miss"


# ── P-02: CARD handler ─────────────────────────────────────────────────────────

class TestCardHandler:
    def _make_plugin_with_backend(self, backend=None):
        

        mock_node = MagicMock()
        mock_node.storage = MagicMock()
        if backend is not None:
            mock_plugins = MagicMock()
            mock_plugins.get_plugin_by_name.return_value = backend
            mock_node._plugins = mock_plugins
        else:
            mock_node._plugins = None

        ctx = MagicMock()
        ctx.node_id = "b" * 64
        ctx.state_dir = Path(tempfile.mkdtemp())
        ctx.plugin_dir = ctx.state_dir
        ctx.subscribe_events = None
        ctx._node = mock_node
        ctx.send_fire_forget = None
        ctx.get_plugin = MagicMock(return_value=backend)

        plugin = PunchholeFrontendPlugin.__new__(PunchholeFrontendPlugin)
        plugin._ctx = ctx
        plugin._config = {}
        plugin._debug = False
        plugin._cache = {}
        plugin._acl = {}
        plugin._backend_ready = True
        import sqlite3
        plugin._db_path = str(ctx.state_dir / "disclosure.db")
        conn = sqlite3.connect(plugin._db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS disclosure_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester TEXT, object_key TEXT, acl_group TEXT, outcome TEXT, ts REAL
        )""")
        conn.commit()
        conn.close()
        return plugin

    @pytest.mark.asyncio
    async def test_card_action_calls_build_card(self):
        """CARD action invokes backend.build_card with requester_node_id."""
        mock_backend = MagicMock()
        card_data = {
            "document_type": "punchhole_card",
            "for_node": "c" * 64,
            "available": [],
        }
        mock_backend.build_card.return_value = card_data

        plugin = self._make_plugin_with_backend(mock_backend)

        from knarr.core.messages import PluginMessage
        msg = MagicMock(spec=PluginMessage)
        msg.plugin_name = "knarr-punchhole"
        msg.action = "CARD"
        msg.node_id = "c" * 64
        msg.payload = json.dumps({"_request_id": "req1"})
        request = {"_request_id": "req1"}

        result = await plugin._handle_card(msg, "127.0.0.1", request, "")
        assert result is True
        mock_backend.build_card.assert_called_once_with("c" * 64)

    @pytest.mark.asyncio
    async def test_card_action_returns_unavailable_when_no_backend(self):
        """CARD action returns unavailable when backend not found."""
        plugin = self._make_plugin_with_backend(backend=None)

        responses_sent = []

        async def fake_send(peer, msg):
            payload = json.loads(msg.payload)
            responses_sent.append(payload)

        plugin._ctx.send_fire_forget = fake_send
        plugin._ctx.get_peers = lambda: []

        from knarr.core.messages import PluginMessage
        msg = MagicMock(spec=PluginMessage)
        msg.plugin_name = "knarr-punchhole"
        msg.action = "CARD"
        msg.node_id = "d" * 64
        msg.payload = json.dumps({"_request_id": "req2"})

        result = await plugin._handle_card(msg, "127.0.0.1", {"_request_id": "req2"}, "")
        assert result is True
        # No send_fire_forget called (no peer resolved), but no crash

    @pytest.mark.asyncio
    async def test_on_inbound_routes_card_action(self):
        """on_inbound with action=CARD dispatches to _handle_card."""
        plugin = self._make_plugin_with_backend(backend=None)
        plugin._handle_card = AsyncMock(return_value=True)

        from knarr.core.messages import PluginMessage
        msg = MagicMock(spec=PluginMessage)
        msg.plugin_name = "knarr-punchhole"
        msg.action = "CARD"
        msg.node_id = "e" * 64
        msg.payload = "{}"

        result = await plugin.on_inbound(msg, "127.0.0.1")
        assert result is True
        plugin._handle_card.assert_called_once()
        call_args = plugin._handle_card.call_args
        assert call_args[0][0] is msg  # msg
        assert call_args[0][1] == "127.0.0.1"  # peer_ip

    @pytest.mark.asyncio
    async def test_on_inbound_ignores_non_punchhole_plugin(self):
        """Messages for other plugins are not handled."""
        plugin = self._make_plugin_with_backend(backend=None)

        from knarr.core.messages import PluginMessage
        msg = MagicMock(spec=PluginMessage)
        msg.plugin_name = "other-plugin"
        msg.action = "CARD"
        msg.node_id = "f" * 64

        result = await plugin.on_inbound(msg, "127.0.0.1")
        assert result is True  # pass-through
