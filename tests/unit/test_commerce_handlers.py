"""Tests for commerce handlers (async closure pattern)."""
import asyncio
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from knarr.commerce.handlers import make_commerce_handlers


@pytest.fixture
def mock_node():
    node = MagicMock()
    node._enqueue_write = AsyncMock()
    node._sync = MagicMock()
    node._sync.enqueue = AsyncMock()
    node.storage = MagicMock()
    node.storage.get_receipt.return_value = None  # default: no existing receipt
    node.storage.get_pubkey_by_node_id.return_value = None  # default: unknown node
    return node


@pytest.fixture
def handlers(mock_node):
    return make_commerce_handlers(mock_node)


def _run(coro):
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestHandleReceipt:
    def test_stores_quality_rating(self, handlers, mock_node):
        item = {"body": {
            "type": "knarr/commerce/receipt", "task_id": "T1",
            "status": "accepted", "timestamp": 0, "quality_rating": 5
        }}
        _run(handlers["knarr/commerce/receipt"](item))
        mock_node._enqueue_write.assert_called_once()
        args = mock_node._enqueue_write.call_args[0]
        assert args[0] == mock_node.storage.update_receipt_quality
        assert args[1] == "T1"
        assert args[2] == 5

    def test_rejected_triggers_credit_note(self, handlers, mock_node):
        mock_node.storage.get_execution_log_entry.return_value = {"price": 10.0}
        item = {"body": {
            "type": "knarr/commerce/receipt", "task_id": "T1",
            "status": "rejected", "timestamp": 0, "refund_requested": True
        }, "from_node": "peer123"}
        _run(handlers["knarr/commerce/receipt"](item))
        mock_node._sync.enqueue.assert_called_once()
        call_kwargs = mock_node._sync.enqueue.call_args[1]
        assert call_kwargs["msg_type"] == "knarr/commerce/credit_note"
        assert call_kwargs["body"]["amount"] == 10.0

    def test_accepted_no_quality_no_writes(self, handlers, mock_node):
        item = {"body": {
            "type": "knarr/commerce/receipt", "task_id": "T1",
            "status": "accepted", "timestamp": 0
        }}
        _run(handlers["knarr/commerce/receipt"](item))
        mock_node._enqueue_write.assert_not_called()

    def test_invalid_receipt_dropped(self, handlers, mock_node):
        item = {"body": {"type": "knarr/commerce/receipt"}}  # missing fields
        _run(handlers["knarr/commerce/receipt"](item))
        mock_node._enqueue_write.assert_not_called()


class TestHandleCreditNote:
    def test_applies_refund(self, handlers, mock_node):
        peer_pubkey = "ab" * 32
        import hashlib
        node_id = hashlib.sha256(bytes.fromhex(peer_pubkey)).hexdigest()
        mock_node.storage.get_pubkey_by_node_id.return_value = peer_pubkey
        mock_node.storage.get_execution_log_entry.return_value = {"price": 20.0}
        item = {"from_node": node_id, "body": {
            "type": "knarr/commerce/credit_note", "amount": 10.0,
            "reason": "other", "timestamp": 0,
            "references": {"task_id": "T1", "original_amount": 20.0}
        }}
        _run(handlers["knarr/commerce/credit_note"](item))
        mock_node._enqueue_write.assert_called_once()
        args = mock_node._enqueue_write.call_args[0]
        assert args[0] == mock_node.storage.update_ledger_refund
        assert args[1] == peer_pubkey
        assert args[2] == 10.0

    def test_inflation_guard_rejects(self, handlers, mock_node):
        mock_node.storage.get_execution_log_entry.return_value = {"price": 10.0}
        item = {"from_node": "peer", "body": {
            "type": "knarr/commerce/credit_note", "amount": 100.0,
            "reason": "other", "timestamp": 0,
            "references": {"task_id": "T1", "original_amount": 10.0}
        }}
        _run(handlers["knarr/commerce/credit_note"](item))
        mock_node._enqueue_write.assert_not_called()

    def test_missing_task_id_rejected(self, handlers, mock_node):
        """F-4 sentinel: credit notes without references.task_id are rejected."""
        item = {"from_node": "peer", "body": {
            "type": "knarr/commerce/credit_note", "amount": 5.0,
            "reason": "other", "timestamp": 0,
            "references": {"original_amount": 10.0}
        }}
        _run(handlers["knarr/commerce/credit_note"](item))
        mock_node._enqueue_write.assert_not_called()

    def test_unknown_node_drops(self, handlers, mock_node):
        # get_pubkey_by_node_id returns None by default (unknown node) — no setup needed
        mock_node.storage.get_execution_log_entry.return_value = {"price": 10.0}
        item = {"from_node": "unknown_node_id", "body": {
            "type": "knarr/commerce/credit_note", "amount": 5.0,
            "reason": "other", "timestamp": 0,
            "references": {"task_id": "T1"}
        }}
        _run(handlers["knarr/commerce/credit_note"](item))
        mock_node._enqueue_write.assert_not_called()


class TestHandleSettleRequest:
    def test_queues_settlement(self, handlers, mock_node):
        item = {"body": {
            "type": "knarr/commerce/settle_request", "current_balance": -100,
            "credit_limit": 200, "provider_wallet": "A" * 44, "timestamp": 0
        }, "from_node": "peer123"}
        _run(handlers["knarr/commerce/settle_request"](item))
        mock_node._enqueue_write.assert_called_once()
        args = mock_node._enqueue_write.call_args[0]
        assert args[0] == mock_node.storage.queue_settlement
        assert args[1] == "settle_request"
        assert args[4] == 1  # priority

    def test_invalid_settle_request_dropped(self, handlers, mock_node):
        item = {"body": {"type": "knarr/commerce/settle_request"}}
        _run(handlers["knarr/commerce/settle_request"](item))
        mock_node._enqueue_write.assert_not_called()


class TestHandleSettlementConfirmation:
    def test_queues_confirmation(self, handlers, mock_node):
        tx_hash = "A" * 88
        # Simulate a matching pending settlement receipt (required by #12 fix)
        mock_node.storage.get_receipt.return_value = {"id": tx_hash, "document_type": "settlement_accepted"}
        item = {"body": {
            "type": "knarr/commerce/settlement_confirmation",
            "tx_hash": tx_hash, "amount_settled": 50.0, "timestamp": 0
        }, "from_node": "peer123"}
        _run(handlers["knarr/commerce/settlement_confirmation"](item))
        mock_node._enqueue_write.assert_called_once()
        args = mock_node._enqueue_write.call_args[0]
        assert args[0] == mock_node.storage.queue_settlement
        assert args[1] == "settlement_confirmation"
        assert args[4] == 0  # priority

    def test_invalid_confirmation_dropped(self, handlers, mock_node):
        item = {"body": {"type": "knarr/commerce/settlement_confirmation"}}
        _run(handlers["knarr/commerce/settlement_confirmation"](item))
        mock_node._enqueue_write.assert_not_called()
