"""Tests for v0.33.0 B-track bug fixes and C-track configurable limiters."""
import asyncio
import hashlib
import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── B1: S-025 LIKE injection fix ──────────────────────────────────

class TestLikeEscaping:
    def test_escape_like_percent(self):
        from knarr.dht.storage import Storage
        s = Storage(":memory:")
        assert s._escape_like("abc%def") == "abc\\%def"

    def test_escape_like_underscore(self):
        from knarr.dht.storage import Storage
        s = Storage(":memory:")
        assert s._escape_like("abc_def") == "abc\\_def"

    def test_escape_like_backslash(self):
        from knarr.dht.storage import Storage
        s = Storage(":memory:")
        assert s._escape_like("abc\\def") == "abc\\\\def"

    def test_escape_like_clean_string(self):
        from knarr.dht.storage import Storage
        s = Storage(":memory:")
        assert s._escape_like("abcdef0123456789") == "abcdef0123456789"

    def test_has_pending_settlement_uses_escape(self):
        """has_pending_settlement uses escaped LIKE to prevent injection."""
        from knarr.dht.storage import Storage
        s = Storage(":memory:")
        # Ensure the method doesn't crash with metacharacters
        result = s.has_pending_settlement("abc%_\\def" + "x" * 24)
        assert result is False  # no pending settlements in empty DB


# ── B3: S-021 Cumulative refund tracking ──────────────────────────

class TestCumulativeRefund:
    def test_get_cumulative_refund_zero_default(self):
        from knarr.dht.storage import Storage
        s = Storage(":memory:")
        result = s.get_cumulative_refund("nonexistent_task")
        assert result == 0.0

    def test_record_and_get_refund(self):
        from knarr.dht.storage import Storage
        s = Storage(":memory:")
        conn = s._get_conn()
        # Add price + refund_total columns (migration safety)
        for col in ["price REAL", "refund_total REAL NOT NULL DEFAULT 0.0"]:
            try:
                conn.execute(f"ALTER TABLE execution_log ADD COLUMN {col}")
            except Exception:
                pass
        conn.execute(
            "INSERT INTO execution_log (job_id, skill_name, caller_node_id, status, wall_time_ms, price) VALUES (?, ?, ?, ?, ?, ?)",
            ("task1", "echo", "caller1", "completed", 100, 10.0)
        )
        conn.commit()

        # Record refunds
        s.record_refund("task1", 3.0)
        assert s.get_cumulative_refund("task1") == 3.0

        s.record_refund("task1", 2.0)
        assert s.get_cumulative_refund("task1") == 5.0

    def test_get_execution_log_entry_includes_refund_total(self):
        from knarr.dht.storage import Storage
        s = Storage(":memory:")
        conn = s._get_conn()
        # Add price + refund_total columns (migration safety)
        for col in ["price REAL", "refund_total REAL NOT NULL DEFAULT 0.0"]:
            try:
                conn.execute(f"ALTER TABLE execution_log ADD COLUMN {col}")
            except Exception:
                pass
        conn.execute(
            "INSERT INTO execution_log (job_id, skill_name, caller_node_id, status, wall_time_ms, price, refund_total) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("task1", "echo", "caller1", "completed", 100, 10.0, 5.0)
        )
        conn.commit()

        entry = s.get_execution_log_entry("task1")
        assert entry is not None
        assert entry["refund_total"] == 5.0
        assert entry["price"] == 10.0


# ── B3: Commerce handler refund cap ──────────────────────────────

class TestCommerceRefundCap:
    def test_refund_capped_at_1x_original(self):
        """Credit note rejected when amount exceeds original price (1x cap)."""
        from knarr.commerce.handlers import make_commerce_handlers

        node = MagicMock()
        node._enqueue_write = AsyncMock()
        node.storage = MagicMock()
        node.storage.get_execution_log_entry.return_value = {"price": 10.0}
        # get_cumulative_refund removed from handler (v0.35.0): 1x per-note cap replaces 2x cumulative cap
        node.storage.get_all_ledger_entries.return_value = [
            {"peer_public_key": "ab" * 32}
        ]

        handlers = make_commerce_handlers(node)
        sender_id = hashlib.sha256(bytes.fromhex("ab" * 32)).hexdigest()

        item = {"body": {
            "type": "knarr/commerce/credit_note",
            "amount": 11.0,  # exceeds 1x cap of 10.0 → rejected
            "reason": "quality_rejection",
            "timestamp": 0,
            "schema_version": "1.0",
            "references": {"task_id": "task1"}
        }, "from_node": sender_id}

        _run(handlers["knarr/commerce/credit_note"](item))
        # Should NOT call _enqueue_write because amount > original price
        node._enqueue_write.assert_not_called()


# ── Stale inbox query ─────────────────────────────────────────────

class TestStaleInboxMessages:
    def test_get_stale_inbox_messages_returns_old_unread(self):
        from knarr.dht.storage import Storage
        import time
        s = Storage(":memory:")
        conn = s._get_conn()

        # Insert some test mail using correct column names (message_id, not item_id)
        old_ts = time.time() - 86400 * 2  # 2 days ago
        conn.execute(
            "INSERT INTO mail_inbox (message_id, from_node, to_node, timestamp, body, msg_type, status, created_at, ttl_expires) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("m1", "sender1", "me", old_ts, '{"text":"hi"}', "text", "unread", old_ts, old_ts + 86400 * 7)
        )
        new_ts = time.time()
        conn.execute(
            "INSERT INTO mail_inbox (message_id, from_node, to_node, timestamp, body, msg_type, status, created_at, ttl_expires) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("m2", "sender2", "me", new_ts, '{"text":"hey"}', "text", "unread", new_ts, new_ts + 86400 * 7)
        )
        conn.commit()

        # Cutoff 24h ago
        cutoff = time.time() - 86400
        stale = s.get_stale_inbox_messages(cutoff, limit=10)
        assert len(stale) == 1
        assert stale[0]["item_id"] == "m1"
        assert stale[0]["from_node"] == "sender1"

    def test_get_stale_inbox_messages_empty(self):
        from knarr.dht.storage import Storage
        import time
        s = Storage(":memory:")
        stale = s.get_stale_inbox_messages(time.time() - 86400, limit=10)
        assert stale == []


# ── C-Track: Configurable limiters ────────────────────────────────

class TestMinimumPriceFloor:
    def test_global_minimum_price_applied(self):
        """Global minimum_price from [skills] config overrides low prices."""
        import types
        from knarr.dht.node import DHTNode

        node = MagicMock(spec=DHTNode)
        node._config = {
            "skills": {"minimum_price": 0.5},
            "pricing": {},
        }
        node._group_engine = None
        node._skill_policies = {}
        node.storage = MagicMock()
        node.storage._get_conn.return_value.execute.return_value.fetchall.return_value = []
        node.storage._get_conn.return_value.execute.return_value.fetchone.return_value = None
        # MagicMock(spec=DHTNode) intercepts _resolve_price_builtin — bind real impl so
        # _resolve_price can delegate to it correctly.
        node._resolve_price_builtin = types.MethodType(DHTNode._resolve_price_builtin, node)

        price, breakdown = DHTNode._resolve_price(node, "deadbeef" * 8, 0.1, "test-skill")
        assert price >= 0.5

    def test_no_minimum_price_default(self):
        """When minimum_price not set, global minimum is 0.0 (existing static floor still applies)."""
        import types
        from knarr.dht.node import DHTNode

        node = MagicMock(spec=DHTNode)
        node._config = {
            "skills": {},
            "pricing": {},
        }
        node._group_engine = None
        node._skill_policies = {}
        node.storage = MagicMock()
        node.storage._get_conn.return_value.execute.return_value.fetchall.return_value = []
        node.storage._get_conn.return_value.execute.return_value.fetchone.return_value = None
        # Bind real _resolve_price_builtin — MagicMock otherwise intercepts and returns empty mock.
        node._resolve_price_builtin = types.MethodType(DHTNode._resolve_price_builtin, node)

        price, breakdown = DHTNode._resolve_price(node, "deadbeef" * 8, 0.0, "free-skill")
        # The pricing system has a static floor of 0.01 by default (from pricing.min_price).
        # The global minimum_price config (default 0.0) doesn't override this existing floor.
        assert price >= 0.0  # no crash, price is non-negative


class TestMaxQueueDepth:
    def test_max_queue_depth_from_config(self):
        """max_queue_depth config sets queue maxsize."""
        # We can't easily test this without constructing a full node,
        # but we can verify the config key is in the whitelist
        from knarr.cli.config import _KNOWN_KEYS
        assert "max_queue_depth" in _KNOWN_KEYS["node"]


class TestDefaultTimeout:
    def test_default_timeout_config_key(self):
        """default_timeout is in config key whitelist."""
        from knarr.cli.config import _KNOWN_KEYS
        assert "default_timeout" in _KNOWN_KEYS["skills"]


class TestEconomyLimits:
    def test_default_soft_limit_config_key(self):
        from knarr.cli.config import _KNOWN_KEYS
        assert "default_soft_limit" in _KNOWN_KEYS["economy"]

    def test_default_hard_limit_config_key(self):
        from knarr.cli.config import _KNOWN_KEYS
        assert "default_hard_limit" in _KNOWN_KEYS["economy"]

    def test_resolve_policy_uses_economy_config(self):
        """_resolve_policy uses [economy] defaults when set."""
        from knarr.dht.node import DHTNode

        node = MagicMock(spec=DHTNode)
        node._config = {
            "economy": {"default_soft_limit": 5.0, "default_hard_limit": -20.0},
            "credit": {},
        }
        node.policy = MagicMock()
        node.policy.initial_credit = 3.0
        node.policy.min_balance = -10.0
        node._skill_policies = {}
        node._group_engine = None
        node._group_policies = []

        ic, mb = DHTNode._resolve_policy(node, "ab" * 32, "echo")
        assert ic == 5.0   # from economy config, not policy default
        assert mb == -20.0  # from economy config, not policy default


class TestMinPeers:
    def test_min_peers_config_key(self):
        from knarr.cli.config import _KNOWN_KEYS
        assert "min_peers" in _KNOWN_KEYS["network"]


class TestEventBusSizeConfig:
    def test_event_bus_size_config_key(self):
        from knarr.cli.config import _KNOWN_KEYS
        assert "event_bus_size" in _KNOWN_KEYS["node"]

    def test_event_bus_debug_config_key(self):
        from knarr.cli.config import _KNOWN_KEYS
        assert "event_bus_debug" in _KNOWN_KEYS["node"]


class TestLimitsExposedInStatus:
    def test_get_status_includes_limits(self):
        """get_status() exposes active limits."""
        from knarr.dht.node import DHTNode

        node = MagicMock(spec=DHTNode)
        node._config = {
            "skills": {"minimum_price": 0.5, "default_timeout": 60},
            "node": {"max_queue_depth": 50},
            "network": {"min_peers": 12, "bootstrap": []},
            "economy": {"default_soft_limit": 5.0, "default_hard_limit": -20.0},
        }
        node.node_info = MagicMock()
        node.node_info.node_id = "test_node"
        node.node_info.host = "0.0.0.0"
        node.node_info.port = 9000
        node._start_time = 0.0
        node._handlers = {}
        node._active_workers = 0
        node._task_slots = 4
        node._task_queue = MagicMock()
        node._task_queue.maxsize = 50
        node._version_gated = False
        node._wallet = ""
        node.policy = MagicMock()
        node.policy.initial_credit = 3.0
        node.policy.min_balance = -10.0
        node.storage = MagicMock()
        node.storage.get_peers.return_value = []
        node.storage.query_all_active_skills.return_value = []
        node.storage.get_recent_tasks.return_value = []

        # Import MIN_PEER_FLOOR
        from knarr.dht.node import MIN_PEER_FLOOR

        status = DHTNode.get_status(node)
        assert "limits" in status
        limits = status["limits"]
        assert limits["minimum_price"] == 0.5
        assert limits["default_timeout"] == 60
        assert limits["max_queue_depth"] == 50
        assert limits["min_peers"] == 12
        assert limits["default_soft_limit"] == 5.0
        assert limits["default_hard_limit"] == -20.0


# ── Config key whitelist completeness ─────────────────────────────

class TestConfigKeyWhitelist:
    def test_all_c_track_keys_whitelisted(self):
        """All C-track config keys are in the whitelist."""
        from knarr.cli.config import _KNOWN_KEYS

        assert "minimum_price" in _KNOWN_KEYS["skills"]
        assert "default_timeout" in _KNOWN_KEYS["skills"]
        assert "max_queue_depth" in _KNOWN_KEYS["node"]
        assert "min_peers" in _KNOWN_KEYS["network"]
        assert "default_soft_limit" in _KNOWN_KEYS["economy"]
        assert "default_hard_limit" in _KNOWN_KEYS["economy"]

    def test_all_prerequisite_keys_whitelisted(self):
        """All prerequisite config keys are in the whitelist."""
        from knarr.cli.config import _KNOWN_KEYS

        assert "event_bus_size" in _KNOWN_KEYS["node"]
        assert "event_bus_debug" in _KNOWN_KEYS["node"]

    def test_mail_stale_hours_whitelisted(self):
        """stale_inbox_hours for mail.inbox_stale is whitelisted."""
        from knarr.cli.config import _KNOWN_KEYS

        assert "stale_inbox_hours" in _KNOWN_KEYS["mail"]


# ── security.auth_failed integration ──────────────────────────────

class TestSecurityAuthFailed:
    def test_check_auth_emits_on_failure(self):
        """_check_auth emits security.auth_failed when token mismatches."""
        from knarr.dashboard.server import CockpitServer

        class FakeBus:
            def __init__(self):
                self.events = []
            def emit(self, event_type, **fields):
                self.events.append({"event": event_type, **fields})

        bus = FakeBus()
        server = MagicMock(spec=CockpitServer)
        server._auth_token = "secret123"
        server._node = MagicMock()
        server._node.bus = bus

        result = CockpitServer._check_auth(server, {"authorization": "Bearer wrong"},
                                            source_ip="10.0.0.1", endpoint="/api/status")
        assert result is False
        assert len(bus.events) == 1
        assert bus.events[0]["event"] == "security.auth_failed"
        assert bus.events[0]["source_ip"] == "10.0.0.1"

    def test_check_auth_no_event_on_success(self):
        """_check_auth does NOT emit when auth succeeds."""
        from knarr.dashboard.server import CockpitServer

        class FakeBus:
            def __init__(self):
                self.events = []
            def emit(self, event_type, **fields):
                self.events.append({"event": event_type, **fields})

        bus = FakeBus()
        server = MagicMock(spec=CockpitServer)
        server._auth_token = "secret123"
        server._node = MagicMock()
        server._node.bus = bus

        result = CockpitServer._check_auth(server, {"authorization": "Bearer secret123"},
                                            source_ip="10.0.0.1", endpoint="/api/status")
        assert result is True
        assert len(bus.events) == 0

    def test_check_auth_no_token_always_passes(self):
        """_check_auth returns True when no auth_token is configured."""
        from knarr.dashboard.server import CockpitServer

        server = MagicMock(spec=CockpitServer)
        server._auth_token = ""

        result = CockpitServer._check_auth(server, {})
        assert result is True
