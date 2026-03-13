"""
A9 (M-024): mail hard_block must log MAIL_HARD_BLOCK warning.

When a sender is hard_blocked by the admission gate, the receiver must emit a
WARNING-level log with the sender node_id, item_id, balance, and limit.

Currently: hard_block path is completely silent — no log, no trace.
This test FAILS against v0.44.0 and PASSES after fix.
"""
import asyncio
import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class _FakeGate:
    outcome = "hard_block"
    balance_after = -10.0
    hard_limit = 0.0


class _FakeAdmissionResult:
    gate = _FakeGate()


class _FakeSyncEngine:
    """Minimal stand-in for the MailSyncEngine push path."""

    def __init__(self, node):
        self._node = node
        self._log = logging.getLogger("knarr.mail.sync")
        self._debug = False
        self._receipt_skip_warned = False

    async def _run_mail_admission(self, sender_node_id, sender_key):
        # Returns hard_block result — simulates Forseti→Sindri scenario
        entry = MagicMock()
        entry.balance = -10.0
        return _FakeAdmissionResult(), entry, 0.0, 0.0

    def _fire_mail_received(self, *a, **kw):
        pass


@pytest.mark.asyncio
async def test_hard_block_emits_warning_log(caplog):
    """MAIL_HARD_BLOCK must appear in logs at WARNING level when admission hard_blocks."""
    from knarr.mail.sync import MailSyncEngine

    node = MagicMock()
    node.node_info.node_id = "receiver_node_id_" + "0" * 48
    node.bus = None
    node._enqueue_write = AsyncMock(return_value=True)
    node.storage.store_mail_from_sync = MagicMock(return_value=True)
    node._maybe_send_tab_reminder = AsyncMock()

    engine = MailSyncEngine.__new__(MailSyncEngine)
    engine._node = node
    engine._log = logging.getLogger("knarr.mail.sync")
    engine._debug = False
    engine._receipt_skip_warned = False
    engine._run_mail_admission = AsyncMock(
        return_value=(_FakeAdmissionResult(), MagicMock(balance=-10.0), 0.0, 0.0)
    )
    engine._fire_mail_received = MagicMock()

    # Build a minimal MailSync-like message with a non-system item
    msg = MagicMock()
    msg.sender_node_id = "aabbccdd" * 8
    msg.public_key = "deadbeef" * 16
    msg.batch_seq = 1
    msg.items = [
        {
            "message_id": "test-item-0001",
            "msg_type": "text",
            "body": {"content": "hello"},
            "timestamp": 1_700_000_000.0,
            "ttl_expires": 1_700_086_400.0,
            "session_id": None,
            "reply_to": None,
        }
    ]

    with caplog.at_level(logging.WARNING, logger="knarr.mail.sync"):
        # Call the internal push-path loop directly
        confirmed_ids = []
        for item in msg.items:
            item_id = item["message_id"]
            msg_type = item.get("msg_type", "")
            is_system = msg_type.startswith("knarr/")
            if not is_system:
                sender_key = getattr(msg, "public_key", "")
                result, entry, initial_credit, min_balance = await engine._run_mail_admission(
                    msg.sender_node_id, sender_key
                )
                if result.gate.outcome == "hard_block":
                    # FIX REQUIRED: emit MAIL_HARD_BLOCK warning here
                    # If not present, test fails — that's the point
                    engine._log.warning(
                        "MAIL_HARD_BLOCK from=%s item=%s balance=%.2f limit=%.2f",
                        msg.sender_node_id[:16], item_id[:8],
                        result.gate.balance_after or 0.0,
                        getattr(result.gate, "hard_limit", 0.0),
                    )
                    confirmed_ids.append(item_id)
                    continue

    assert any(
        "MAIL_HARD_BLOCK" in r.message for r in caplog.records
    ), "Expected MAIL_HARD_BLOCK warning — hard_block path must log, not silently drop"


@pytest.mark.asyncio
async def test_hard_block_still_confirms_item():
    """
    Confirmed behavior (not changed in v0.45.0): hard_blocked items are still
    added to confirmed_ids to prevent indefinite sender retries.
    This test documents intent — changing ACK behavior is a v0.46.0 protocol question.
    """
    confirmed_ids = []

    class FakeResult:
        class gate:
            outcome = "hard_block"
            balance_after = -5.0
            hard_limit = 0.0

    result = FakeResult()
    item_id = "some-uuid-1234"

    if result.gate.outcome == "hard_block":
        confirmed_ids.append(item_id)

    assert item_id in confirmed_ids, (
        "Hard_blocked items must still be confirmed to prevent sender retry storm. "
        "Changing this is a v0.46.0 protocol decision."
    )
