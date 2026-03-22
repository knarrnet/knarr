"""SEC-04: Settlement confirmation receipt records final_balance=0.0.

BUG: _handle_settlement_confirmation() reads ledger balance AFTER enqueuing
the zero-write (which is async). The read returns the pre-zero balance.
write_settlement_processed() receives a non-zero final_balance in the audit trail.

FIX: Pass 0.0 directly as final_balance argument to write_settlement_processed(),
since the settlement has zeroed the ledger conceptually at this point.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, call


def _make_settlement_confirmation_node():
    """Create a minimal DHTNode stub for testing _handle_settlement_confirmation."""
    from knarr.dht.node import DHTNode
    from knarr.core.models import NodeInfo
    from nacl.signing import SigningKey

    node = MagicMock(spec=DHTNode)
    node.node_info = NodeInfo(node_id="aa" * 32, host="127.0.0.1", port=9000)
    node._public_key_hex = "bb" * 32
    node._signing_key = SigningKey.generate()
    node._debug = False
    node.bus = MagicMock()

    storage = MagicMock()
    # Simulate prior balance of -100.0 (they owe us 100)
    storage.get_ledger_balance = MagicMock(return_value=-100.0)
    storage.write_receipt = MagicMock()
    node.storage = storage

    node._enqueue_write = AsyncMock()
    node._resolve_settlement_peer_key = MagicMock(return_value="cc" * 32)
    return node


@pytest.mark.asyncio
async def test_settlement_confirmation_final_balance_is_zero():
    """write_settlement_processed() must receive final_balance=0.0, not the stale balance."""
    from knarr.dht.node import DHTNode

    node = _make_settlement_confirmation_node()
    peer_key = "cc" * 32
    prior_balance = -100.0  # stale balance that storage still returns

    # Simulate incoming settlement confirmation
    from nacl.signing import SigningKey
    from knarr.core.proof import sign_document

    proposer_sk = SigningKey.generate()
    vm = f"did:knarr:{'dd' * 32}#key-1"
    proof_doc = sign_document({"type": "settle_doc", "amount": 100.0}, proposer_sk, vm)

    item = {
        "id": "test-confirm-001",
        "from_node": "dd" * 32,
        "body": {
            "type": "knarr/commerce/settlement_confirmation",
            "amount_settled": 100.0,
            "accepted_receipt_id": "receipt-001",
            "settle_request_ref": "req-001",
            "peer_key": peer_key,
            "counterparty_key": peer_key,
            "proof": proof_doc.get("proof"),
        }
    }

    # Capture the final_balance passed to write_settlement_processed
    captured_final_balance = []

    async def fake_write_settlement_processed(**kwargs):
        captured_final_balance.append(kwargs.get("final_balance"))
        return "receipt-id-001"

    with patch("knarr.commerce.settlement_execution.write_settlement_processed", side_effect=fake_write_settlement_processed):
        await DHTNode._handle_settlement_confirmation(node, item)

    assert len(captured_final_balance) > 0, (
        "write_settlement_processed was not called"
    )
    assert captured_final_balance[0] == 0.0, (
        f"SEC-04: write_settlement_processed received final_balance={captured_final_balance[0]}, "
        f"expected 0.0. Storage would return prior_balance={prior_balance} if read directly. "
        f"Fix: pass 0.0 directly, not the stale read-after-enqueue balance."
    )


@pytest.mark.asyncio
async def test_settlement_confirmation_enqueues_ledger_zero():
    """_handle_settlement_confirmation() must enqueue the ledger zero-write."""
    from knarr.dht.node import DHTNode

    node = _make_settlement_confirmation_node()
    peer_key = "cc" * 32

    from nacl.signing import SigningKey
    from knarr.core.proof import sign_document

    proposer_sk = SigningKey.generate()
    vm = f"did:knarr:{'dd' * 32}#key-1"
    proof_doc = sign_document({"type": "settle_doc", "amount": 100.0}, proposer_sk, vm)

    item = {
        "id": "test-confirm-002",
        "from_node": "dd" * 32,
        "body": {
            "type": "knarr/commerce/settlement_confirmation",
            "amount_settled": 100.0,
            "accepted_receipt_id": "receipt-002",
            "settle_request_ref": "req-002",
            "peer_key": peer_key,
            "counterparty_key": peer_key,
            "proof": proof_doc.get("proof"),
        }
    }

    with patch("knarr.commerce.settlement_execution.write_settlement_processed", return_value="receipt-id-002"):
        await DHTNode._handle_settlement_confirmation(node, item)

    # Verify ledger zero-write was enqueued
    assert node._enqueue_write.called, (
        "SEC-04: _enqueue_write (ledger zero) was not called"
    )
