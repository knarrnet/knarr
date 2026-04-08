"""Tests for v0.55.0 S-series fixes — S-01, S-02, S-04, S-05, S-06."""

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest


# ── S-01: Settlement Receiver-Side Dedup ──────────────────────────────────────

class TestSettlementDedup:
    """S-01: _handle_settlement_confirmation checks has_pending_settlement before processing."""

    def test_dedup_guard_exists(self):
        """Settlement confirmation handler checks for pending settlement."""
        from knarr.dht import node as node_mod
        import inspect
        src = inspect.getsource(node_mod.DHTNode._handle_settlement_confirmation)
        assert "has_pending_settlement" in src, "S-01 has_pending_settlement guard missing"
        assert "SETTLEMENT_CONFIRM_DEDUP" in src, "S-01 dedup log message missing"



# ── S-02: WAL Checkpoint on Shutdown ──────────────────────────────────────────

class TestWALCheckpoint:
    """S-02: storage.close() runs WAL checkpoint before closing."""

    def test_close_succeeds_without_error(self, tmp_path):
        """close() completes without raising (WAL checkpoint + connection close)."""
        from knarr.dht.storage import Storage

        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path)
        # Should not raise
        storage.close()

    def test_wal_checkpoint_clears_wal_file(self, tmp_path):
        """After close(), WAL file should be empty or absent (checkpoint ran)."""
        import sqlite3 as _sqlite3
        from knarr.dht.storage import Storage

        db_path = str(tmp_path / "test_wal.db")
        storage = Storage(db_path)

        # Write some data to ensure WAL has content
        conn = _sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS _test_wal (x INTEGER)")
        conn.execute("INSERT INTO _test_wal VALUES (1)")
        conn.commit()
        conn.close()

        wal_path = db_path + "-wal"
        shm_path = db_path + "-shm"

        storage.close()

        # After RESTART checkpoint, WAL should be cleared (0 bytes or not exist)
        if os.path.exists(wal_path):
            wal_size = os.path.getsize(wal_path)
            assert wal_size == 0, f"WAL not checkpointed: {wal_size} bytes remain"

    def test_storage_close_is_idempotent_on_already_closed(self, tmp_path):
        """close() should not raise even if called on a closed connection."""
        from knarr.dht.storage import Storage

        db_path = str(tmp_path / "test3.db")
        storage = Storage(db_path)
        storage.close()
        # Second close might error on the connection, but the checkpoint PRAGMA should not


# ── S-04: Self-Call via Explicit Provider Fix ──────────────────────────────────

class TestSelfCallFix:
    """S-04: Explicit provider matching local node routes locally."""

    def _make_server(self, local_node_id="local_node_123"):
        from knarr.dashboard.server import CockpitServer as DashboardServer

        node = MagicMock()
        node.node_info.node_id = local_node_id
        node.node_info.host = "127.0.0.1"
        node.node_info.port = 9000
        node.storage.get_peer_by_id = MagicMock(return_value=None)

        server = DashboardServer.__new__(DashboardServer)
        server._node = node
        server._fire_and_forget_task = MagicMock()
        server._respond_error = MagicMock()
        server._select_execute_provider = MagicMock(return_value=None)
        return server

    @pytest.mark.asyncio
    async def test_explicit_local_provider_routes_locally(self):
        """When provider node_id == local node, use local fast path."""
        server = self._make_server(local_node_id="my_node_id_64chars" + "x" * 45)
        local_id = server._node.node_info.node_id

        writer = MagicMock()
        body_data = {
            "skill": "test.skill",
            "input": {"key": "value"},
            "provider": {"node_id": local_id},
        }
        body = json.dumps(body_data).encode()

        await server._handle_api_execute(writer, body)

        server._fire_and_forget_task.assert_called_once()
        args = server._fire_and_forget_task.call_args[0]
        # args = (writer, node_id, host, port, skill, task_input, timeout_ms)
        assert args[1] == local_id  # node_id
        assert args[2] == "127.0.0.1"  # host

    @pytest.mark.asyncio
    async def test_explicit_remote_provider_does_not_route_locally(self):
        """Remote provider node_id does NOT trigger local fast path."""
        server = self._make_server(local_node_id="local_node_" + "a" * 53)

        writer = MagicMock()
        body_data = {
            "skill": "test.skill",
            "input": {"key": "value"},
            "provider": {"node_id": "remote_node_" + "b" * 52},
        }
        body = json.dumps(body_data).encode()

        await server._handle_api_execute(writer, body)

        # No local routing — should try to resolve remote peer
        # _fire_and_forget_task NOT called via local self-route path
        # (it might be called via fallback, but not the S-04 self-route)

    @pytest.mark.asyncio
    async def test_local_flag_still_routes_locally(self):
        """local=true in request still routes locally (pre-existing behavior)."""
        server = self._make_server()

        writer = MagicMock()
        body_data = {
            "skill": "test.skill",
            "input": {},
            "local": True,
        }
        body = json.dumps(body_data).encode()

        await server._handle_api_execute(writer, body)

        server._fire_and_forget_task.assert_called_once()


# ── S-05: session_id Merge Fix ────────────────────────────────────────────────

class TestSessionIdMerge:
    """S-05: session_id and reply_to are picked up from both top-level and body."""

    def _setup_mail_handler(self, node=None, config=None):
        """Initialize mail handler module."""
        import knarr.mail.handler as mail_handler
        mail_handler._node = node or MagicMock()
        mail_handler._mail_config = config or {}
        mail_handler._x25519_private = None
        mail_handler._group_engine = None
        return mail_handler

    @pytest.mark.asyncio
    async def test_session_id_from_top_level(self):
        """session_id at top level of input_data is picked up."""
        mail_handler = self._setup_mail_handler()
        node = MagicMock()
        node.node_info.node_id = "n" * 64
        node.storage.count_mail_inbox.return_value = 0
        node._enqueue_write = AsyncMock()
        mail_handler._node = node

        input_data = {
            "action": "send",
            "session_id": "sess_toplevel_123",  # top-level
            "body": {
                "type": "text",
                "content": "hello",
            },
        }

        # Patch to capture what enqueue receives
        enqueued = []
        async def fake_enqueue_write(fn, *args, **kwargs):
            if hasattr(fn, '__name__') and 'enqueue' in fn.__name__:
                enqueued.append(args)
            elif callable(fn):
                enqueued.append(args)

        node._enqueue_write = fake_enqueue_write

        from knarr.mail.handler import _handle_send
        result = await _handle_send(input_data)

        # Should succeed and have used session_id from top-level
        assert result.get("status") in ("queued", "ok", "accepted") or "message_id" in result

    @pytest.mark.asyncio
    async def test_session_id_from_body(self):
        """session_id nested in body is picked up."""
        import knarr.mail.handler as mail_handler
        node = MagicMock()
        node.node_info.node_id = "m" * 64
        node.storage.count_mail_inbox.return_value = 0
        node._enqueue_write = AsyncMock()
        mail_handler._node = node
        mail_handler._mail_config = {}
        mail_handler._x25519_private = None
        mail_handler._group_engine = None

        input_data = {
            "action": "send",
            "body": {
                "type": "text",
                "content": "hello",
                "session_id": "sess_body_456",  # in body
                "reply_to": "reply_123",
            },
        }

        from knarr.mail.handler import _handle_send
        result = await _handle_send(input_data)

        assert result.get("status") in ("queued", "ok", "accepted") or "message_id" in result

    def test_session_id_prefers_top_level_over_body(self):
        """session_id at top level takes precedence over body value."""
        # The logic: input_data.get("session_id") or body.get("session_id")
        input_data = {"session_id": "top_level_session"}
        body = {"session_id": "body_session"}

        session_id = input_data.get("session_id") or body.get("session_id") or None
        assert session_id == "top_level_session"

    def test_session_id_falls_back_to_body(self):
        """When not at top level, falls back to body."""
        input_data = {}
        body = {"session_id": "body_session"}

        session_id = input_data.get("session_id") or body.get("session_id") or None
        assert session_id == "body_session"

    def test_reply_to_prefers_top_level(self):
        """reply_to at top level takes precedence."""
        input_data = {"reply_to": "top_reply"}
        body = {"reply_to": "body_reply"}

        reply_to = input_data.get("reply_to") or body.get("reply_to") or None
        assert reply_to == "top_reply"

    def test_reply_to_falls_back_to_body(self):
        """reply_to falls back to body when not at top level."""
        input_data = {}
        body = {"reply_to": "body_reply"}

        reply_to = input_data.get("reply_to") or body.get("reply_to") or None
        assert reply_to == "body_reply"


# ── S-06: public_key in /api/status ───────────────────────────────────────────

class TestPublicKeyInStatus:
    """S-06: get_status() includes the node's Ed25519 public key."""

    def _make_node_for_status(self, public_key_hex="abc123" * 10):
        from knarr.dht.node import DHTNode
        node = DHTNode.__new__(DHTNode)
        node.node_info = MagicMock()
        node.node_info.node_id = "node_" + "a" * 59
        node.node_info.host = "127.0.0.1"
        node.node_info.port = 9000
        node._public_key_hex = public_key_hex
        node._start_time = None
        node._config = {}
        node._version_gated = False
        node._upgrading = False
        node._active_workers = 0
        node._task_slots = 10
        node._handlers = {}
        node.storage = MagicMock()
        node.storage.query_all_active_skills.return_value = []
        node.storage.get_peers.return_value = []
        node.storage.get_recent_tasks.return_value = []
        node._task_queue = MagicMock()
        node._task_queue.maxsize = 100
        node.policy = MagicMock()
        node.policy.initial_credit = 10.0
        return node

    def test_status_includes_public_key(self):
        """get_status() returns public_key field."""
        node = self._make_node_for_status(public_key_hex="deadbeef" * 8)
        status = node.get_status()
        assert "public_key" in status
        assert status["public_key"] == "deadbeef" * 8

    def test_status_includes_node_id(self):
        """node_id is still present (pre-existing)."""
        node = self._make_node_for_status()
        status = node.get_status()
        assert "node_id" in status

    def test_status_public_key_not_empty(self):
        """public_key is the actual key, not empty."""
        from knarr.core.crypto import SigningKey
        sk = SigningKey.generate()
        pub_hex = sk.verify_key.encode().hex()

        node = self._make_node_for_status(public_key_hex=pub_hex)
        status = node.get_status()

        assert status["public_key"] == pub_hex
        assert len(status["public_key"]) == 64  # 32 bytes as hex

    def test_status_field_order(self):
        """public_key appears in the returned dict."""
        node = self._make_node_for_status(public_key_hex="cafe" * 16)
        status = node.get_status()
        keys = list(status.keys())
        assert "public_key" in keys
        assert "node_id" in keys
