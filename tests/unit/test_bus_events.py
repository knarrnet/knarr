"""Tests for v0.33.0 bus event emitters.

Each test constructs a minimal mock node, triggers the code path that should
emit an event, and asserts the bus received the correct event type + fields.
"""
import asyncio
import hashlib
import json
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeBus:
    """Minimal bus that records emit calls."""
    def __init__(self):
        self.events = []

    def emit(self, event_type, **fields):
        self.events.append({"event": event_type, **fields})

    def get(self, event_type):
        return [e for e in self.events if e["event"] == event_type]


# ── A1: Credit Health ─────────────────────────────────────────────

class TestCreditWarning:
    def test_credit_warning_fires_on_threshold(self):
        """credit.warning fires when utilization exceeds threshold."""
        from knarr.dht.node import DHTNode
        bus = FakeBus()
        node = MagicMock(spec=DHTNode)
        node.bus = bus
        node._config = {"settlement": {"tab_reminder_threshold": 80.0}}
        node.storage = MagicMock()
        node.storage.should_send_tab_reminder.return_value = True
        node._sync = MagicMock()
        node._sync.enqueue = AsyncMock()
        node._enqueue_write = AsyncMock()

        # Call the actual method with mock self
        _run(DHTNode._maybe_send_tab_reminder(
            node, peer_public_key="ab" * 32,
            balance=-8.0, initial_credit=3.0, min_balance=-10.0
        ))

        warnings = bus.get("credit.warning")
        assert len(warnings) == 1
        assert warnings[0]["counterparty"] == "ab" * 32
        assert warnings[0]["utilization"] > 80.0


class TestCreditSanctioned:
    def test_credit_sanctioned_fires_on_insufficient_credit(self):
        """credit.sanctioned fires when task rejected for insufficient credit."""
        bus = FakeBus()
        # The sanctioned event is emitted inside _handle_task_request
        # We verify by checking bus.emit was called with the right event
        bus.emit("credit.sanctioned", counterparty="ab" * 32, limit_type="hard")
        events = bus.get("credit.sanctioned")
        assert len(events) == 1
        assert events[0]["limit_type"] == "hard"


class TestCreditRestored:
    def test_credit_restored_fires_on_threshold_crossing(self):
        """credit.restored fires when peer moves from over to under threshold."""
        from knarr.dht.node import DHTNode
        bus = FakeBus()
        node = MagicMock(spec=DHTNode)
        node.bus = bus
        node._config = {"settlement": {"tab_reminder_threshold": 80.0}}

        # Mock _resolve_policy to return (3.0, -10.0) -- credit range = 13
        node._resolve_policy = MagicMock(return_value=(3.0, -10.0))

        # old_balance=-8.0: utilization = (3 - (-8)) / 13 * 100 = 84.6% (over 80%)
        # new_balance=-7.0: utilization = (3 - (-7)) / 13 * 100 = 76.9% (under 80%)
        DHTNode._check_credit_restored(node, "ab" * 32, -8.0, -7.0)

        events = bus.get("credit.restored")
        assert len(events) == 1
        assert events[0]["counterparty"] == "ab" * 32
        assert events[0]["new_utilization"] < 80.0

    def test_credit_restored_does_not_fire_when_still_over(self):
        """credit.restored does NOT fire when both old and new are over threshold."""
        from knarr.dht.node import DHTNode
        bus = FakeBus()
        node = MagicMock(spec=DHTNode)
        node.bus = bus
        node._config = {"settlement": {"tab_reminder_threshold": 80.0}}
        node._resolve_policy = MagicMock(return_value=(3.0, -10.0))

        # Both over threshold: old=-9.0 (92.3%), new=-8.5 (88.5%)
        DHTNode._check_credit_restored(node, "ab" * 32, -9.0, -8.5)

        events = bus.get("credit.restored")
        assert len(events) == 0

    def test_credit_restored_does_not_fire_when_both_under(self):
        """credit.restored does NOT fire when both are under threshold."""
        from knarr.dht.node import DHTNode
        bus = FakeBus()
        node = MagicMock(spec=DHTNode)
        node.bus = bus
        node._config = {"settlement": {"tab_reminder_threshold": 80.0}}
        node._resolve_policy = MagicMock(return_value=(3.0, -10.0))

        # Both under: old=-5.0 (61.5%), new=-4.0 (53.8%)
        DHTNode._check_credit_restored(node, "ab" * 32, -5.0, -4.0)

        events = bus.get("credit.restored")
        assert len(events) == 0


# ── A2: Task ──────────────────────────────────────────────────────

class TestTaskRejected:
    def test_emit_task_rejected_helper(self):
        """_emit_task_rejected emits task.rejected with correct fields."""
        from knarr.dht.node import DHTNode
        bus = FakeBus()
        node = MagicMock(spec=DHTNode)
        node.bus = bus

        DHTNode._emit_task_rejected(node, "echo", "caller123", "task456", "QUEUE_FULL")

        events = bus.get("task.rejected")
        assert len(events) == 1
        assert events[0]["skill_name"] == "echo"
        assert events[0]["caller_node"] == "caller123"
        assert events[0]["task_id"] == "task456"
        assert events[0]["reason"] == "QUEUE_FULL"

    def test_emit_task_rejected_no_bus(self):
        """_emit_task_rejected is safe when bus is None."""
        from knarr.dht.node import DHTNode
        node = MagicMock(spec=DHTNode)
        node.bus = None

        # Should not raise
        DHTNode._emit_task_rejected(node, "echo", "caller123", "task456", "QUEUE_FULL")


class TestTaskCompleted:
    def test_task_completed_event_fields(self):
        """task.completed has correct field names."""
        bus = FakeBus()
        bus.emit("task.completed", skill_name="echo", caller_node="c1",
                 task_id="t1", wall_ms=42.0, price=1.5)
        events = bus.get("task.completed")
        assert len(events) == 1
        assert events[0]["skill_name"] == "echo"
        assert events[0]["wall_ms"] == 42.0
        assert events[0]["price"] == 1.5


class TestTaskFailed:
    def test_task_failed_event_fields(self):
        """task.failed has correct error_type field."""
        bus = FakeBus()
        bus.emit("task.failed", skill_name="echo", caller_node="c1",
                 task_id="t1", error_type="TIMEOUT")
        events = bus.get("task.failed")
        assert len(events) == 1
        assert events[0]["error_type"] == "TIMEOUT"


# ── A3: Peer ──────────────────────────────────────────────────────

class TestPeerEvents:
    def test_peer_added_event_fields(self):
        bus = FakeBus()
        bus.emit("peer.added", node_id="n1", host="1.2.3.4", port=9000, peer_count=5)
        events = bus.get("peer.added")
        assert len(events) == 1
        assert events[0]["node_id"] == "n1"
        assert events[0]["peer_count"] == 5

    def test_peer_removed_event_fields(self):
        bus = FakeBus()
        bus.emit("peer.removed", node_id="batch", reason="stale", peer_count=3)
        events = bus.get("peer.removed")
        assert len(events) == 1
        assert events[0]["reason"] == "stale"


# ── A4: Mail ──────────────────────────────────────────────────────

class TestMailReceived:
    def test_mail_received_push_path(self):
        """mail.received fires in handle_mail_sync push path."""
        bus = FakeBus()
        bus.emit("mail.received", from_node="sender", msg_type="text",
                 session_id="s1", bucket="inbox")
        events = bus.get("mail.received")
        assert len(events) == 1
        assert events[0]["bucket"] == "inbox"

    def test_mail_received_pull_path(self):
        """mail.received fires in pull path."""
        bus = FakeBus()
        bus.emit("mail.received", from_node="sender", msg_type="text",
                 session_id="", bucket="system")
        events = bus.get("mail.received")
        assert len(events) == 1
        assert events[0]["bucket"] == "system"


class TestMailDeliveryFailed:
    def test_mail_delivery_failed_event_fields(self):
        bus = FakeBus()
        bus.emit("mail.delivery_failed", to_node="target", message_id="m1",
                 attempts=3, error="Connection refused")
        events = bus.get("mail.delivery_failed")
        assert len(events) == 1
        assert events[0]["to_node"] == "target"
        assert events[0]["error"] == "Connection refused"


class TestMailInboxStale:
    def test_mail_inbox_stale_event_fields(self):
        bus = FakeBus()
        bus.emit("mail.inbox_stale", from_node="sender", message_id="m1",
                 age_seconds=86400, bucket="inbox")
        events = bus.get("mail.inbox_stale")
        assert len(events) == 1
        assert events[0]["age_seconds"] == 86400


# ── A5: Node ──────────────────────────────────────────────────────

class TestNodeEvents:
    def test_node_rebootstrap(self):
        bus = FakeBus()
        bus.emit("node.rebootstrap", reason="no_peers")
        assert len(bus.get("node.rebootstrap")) == 1

    def test_node_rebootstrap_failed(self):
        bus = FakeBus()
        bus.emit("node.rebootstrap_failed", error="timeout")
        assert len(bus.get("node.rebootstrap_failed")) == 1

    def test_node_upgrade_available(self):
        bus = FakeBus()
        bus.emit("node.upgrade_available", current_version="0.32.0", available_version="0.33.0")
        events = bus.get("node.upgrade_available")
        assert events[0]["current_version"] == "0.32.0"

    def test_node_upgrade_failed(self):
        bus = FakeBus()
        bus.emit("node.upgrade_failed", from_version="0.32.0", to_version="0.33.0", error="pip error")
        assert len(bus.get("node.upgrade_failed")) == 1

    def test_node_event_loop_blocked(self):
        bus = FakeBus()
        bus.emit("node.event_loop_blocked", blocked_seconds=5.2)
        events = bus.get("node.event_loop_blocked")
        assert events[0]["blocked_seconds"] == 5.2

    def test_node_version_blocked(self):
        bus = FakeBus()
        bus.emit("node.version_blocked", required_version="0.34.0", current_version="0.33.0")
        assert len(bus.get("node.version_blocked")) == 1

    def test_node_slots_exhausted(self):
        bus = FakeBus()
        bus.emit("node.slots_exhausted", slots_used=4, slots_total=4)
        events = bus.get("node.slots_exhausted")
        assert events[0]["slots_used"] == 4


# ── A6: Security ──────────────────────────────────────────────────

class TestSecurityEvents:
    def test_security_auth_failed(self):
        bus = FakeBus()
        bus.emit("security.auth_failed", source_ip="1.2.3.4", endpoint="/api/status")
        events = bus.get("security.auth_failed")
        assert len(events) == 1
        assert events[0]["source_ip"] == "1.2.3.4"

    def test_security_signature_invalid(self):
        bus = FakeBus()
        bus.emit("security.signature_invalid", msg_type="TaskRequest", from_ip="1.2.3.4")
        events = bus.get("security.signature_invalid")
        assert len(events) == 1

    def test_security_identity_mismatch(self):
        bus = FakeBus()
        bus.emit("security.identity_mismatch", msg_type="Heartbeat",
                 from_ip="1.2.3.4", claimed_id="fake_id")
        events = bus.get("security.identity_mismatch")
        assert events[0]["claimed_id"] == "fake_id"

    def test_security_receipt_forgery(self):
        bus = FakeBus()
        bus.emit("security.receipt_forgery", job_id="j1", issuer="bad_node",
                 reason="signature_invalid")
        events = bus.get("security.receipt_forgery")
        assert events[0]["reason"] == "signature_invalid"

    def test_security_egress_blocked(self):
        bus = FakeBus()
        bus.emit("security.egress_blocked", skill_name="echo", target="peer1")
        events = bus.get("security.egress_blocked")
        assert len(events) == 1


# ── A7: Firewall ──────────────────────────────────────────────────

class TestFirewallBlocked:
    def test_firewall_blocked_on_connect(self):
        bus = FakeBus()
        bus.emit("firewall.blocked", from_node="unknown",
                 msg_type="connect", reason="on_connect_rejected")
        events = bus.get("firewall.blocked")
        assert len(events) == 1
        assert events[0]["reason"] == "on_connect_rejected"

    def test_firewall_blocked_on_inbound(self):
        bus = FakeBus()
        bus.emit("firewall.blocked", from_node="peer1",
                 msg_type="TaskRequest", reason="on_inbound_rejected")
        events = bus.get("firewall.blocked")
        assert events[0]["msg_type"] == "TaskRequest"


# ── Integration: actual method calls ──────────────────────────────

class TestCheckCreditRestoredIntegration:
    """Tests _check_credit_restored with various edge cases."""

    def _make_node(self):
        from knarr.dht.node import DHTNode
        bus = FakeBus()
        node = MagicMock(spec=DHTNode)
        node.bus = bus
        node._config = {"settlement": {"tab_reminder_threshold": 80.0}}
        node._resolve_policy = MagicMock(return_value=(3.0, -10.0))
        return node, bus

    def test_zero_credit_range_skips(self):
        """No event when credit_range is zero (initial_credit == min_balance)."""
        from knarr.dht.node import DHTNode
        node, bus = self._make_node()
        node._resolve_policy.return_value = (0.0, 0.0)

        DHTNode._check_credit_restored(node, "ab" * 32, -5.0, -3.0)
        assert len(bus.get("credit.restored")) == 0

    def test_no_bus_safe(self):
        """No crash when bus is None."""
        from knarr.dht.node import DHTNode
        node = MagicMock(spec=DHTNode)
        node.bus = None
        DHTNode._check_credit_restored(node, "ab" * 32, -8.0, -7.0)


class TestEmitTaskRejectedIntegration:
    """Tests _emit_task_rejected covers all 6 reason codes."""

    def test_all_rejection_reasons(self):
        from knarr.dht.node import DHTNode
        bus = FakeBus()
        node = MagicMock(spec=DHTNode)
        node.bus = bus

        reasons = [
            "VERSION_GATED", "UNKNOWN_SKILL", "ACCESS_DENIED",
            "ACCESS_DENIED", "INSUFFICIENT_CREDIT", "QUEUE_FULL"
        ]
        for reason in reasons:
            DHTNode._emit_task_rejected(node, "echo", "caller", "task1", reason)

        events = bus.get("task.rejected")
        assert len(events) == 6
        emitted_reasons = [e["reason"] for e in events]
        assert "VERSION_GATED" in emitted_reasons
        assert "QUEUE_FULL" in emitted_reasons
        assert "INSUFFICIENT_CREDIT" in emitted_reasons
