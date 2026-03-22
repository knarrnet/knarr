"""CR-01: BCW payment.finalized.* bus event applies ledger credit.

BUG: BCW plugin emits payment.finalized.* events when on-chain payments confirm.
No subscriber in node.py. On-chain deposits produce zero ledger credit.

FIX: _bcw_credit_loop subscribes to payment.finalized.* events, resolves wallet
to node_id, then credits ledger via settlement write path.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, call


def _make_handler(wallet_map=None, pubkey_map=None):
    """Create a BCW credit handler with controlled storage stubs."""
    from knarr.commerce.bcw_credit import make_payment_finalized_handler
    from knarr.core.models import NodeInfo

    node = MagicMock()
    node._debug = False
    node._config = {}  # empty config → default conversion rate 1.0
    node.node_info = NodeInfo(node_id="aa" * 32, host="127.0.0.1", port=9000)
    node._enqueue_write = AsyncMock()

    bus = MagicMock()
    bus.emit = MagicMock()
    node.bus = bus

    storage = MagicMock()

    # wallet → node_id
    def _get_node_by_wallet(wallet):
        return (wallet_map or {}).get(wallet)

    # node_id → pubkey
    def _get_pubkey_by_node_id(nid):
        return (pubkey_map or {}).get(nid)

    storage.get_node_by_wallet = _get_node_by_wallet
    storage.get_pubkey_by_node_id = _get_pubkey_by_node_id
    storage.get_receipt = MagicMock(return_value=None)  # no existing dedup entry
    storage.write_receipt = MagicMock()
    node.storage = storage

    handler = make_payment_finalized_handler(node)
    return handler, node


@pytest.mark.asyncio
async def test_valid_payment_credits_ledger():
    """payment.finalized.* with resolvable wallet applies ledger credit."""
    wallet = "ABC123"
    node_id = "bb" * 32
    peer_key = "cc" * 32

    handler, node = _make_handler(
        wallet_map={wallet: node_id},
        pubkey_map={node_id: peer_key},
    )

    event = {
        "event": "payment.finalized.solana",
        "chain_id": "solana-mainnet",
        "from_address": wallet,
        "to_address": "DEF456",
        "amount": 50.0,
        "decimals": 0,  # whole tokens — no lamport division; at rate=1.0 → 50.0 credits
        "tx_hash": "tx" * 32,
        "tx_index": 0,
    }

    await handler(event)

    # Verify ledger credit was enqueued
    node._enqueue_write.assert_called_once()
    call_args = node._enqueue_write.call_args
    # First arg = storage method, second = peer_key, third = amount_credits
    assert call_args[0][1] == peer_key, f"Expected peer_key={peer_key[:16]}, got {call_args[0][1][:16]}"
    assert call_args[0][2] == 50.0, f"Expected amount=50.0, got {call_args[0][2]}"


@pytest.mark.asyncio
async def test_unresolvable_wallet_logs_warning_no_crash():
    """payment.finalized.* with unresolvable wallet logs warning, no crash, no credit."""
    handler, node = _make_handler(wallet_map={}, pubkey_map={})

    event = {
        "event": "payment.finalized.solana",
        "chain_id": "solana-mainnet",
        "from_address": "UNKNOWN_WALLET",
        "amount": 100.0,
        "tx_hash": "tx" * 32,
        "tx_index": 0,
    }

    # Should not raise
    await handler(event)

    # No ledger credit should be applied
    node._enqueue_write.assert_not_called()


@pytest.mark.asyncio
async def test_credit_applied_event_emitted():
    """settlement.credit_applied event emitted after successful credit."""
    wallet = "WALLET_XYZ"
    node_id = "dd" * 32
    peer_key = "ee" * 32

    handler, node = _make_handler(
        wallet_map={wallet: node_id},
        pubkey_map={node_id: peer_key},
    )

    event = {
        "event": "payment.finalized.solana",
        "chain_id": "solana-mainnet",
        "from_address": wallet,
        "amount": 25.0,
        "decimals": 0,  # whole tokens at rate=1.0 → 25.0 credits
        "tx_hash": "txhash123",
        "tx_index": 0,
    }

    await handler(event)

    # Verify settlement.credit_applied was emitted
    node.bus.emit.assert_called_once()
    emit_args = node.bus.emit.call_args
    assert emit_args[0][0] == "settlement.credit_applied", (
        f"Expected settlement.credit_applied, got {emit_args[0][0]}"
    )
    kwargs = emit_args[1]
    assert kwargs.get("amount") == 25.0
    assert kwargs.get("chain_id") == "solana-mainnet"
    assert kwargs.get("node_id") == node_id


@pytest.mark.asyncio
async def test_zero_amount_skipped():
    """payment.finalized.* with amount=0 must be skipped."""
    wallet = "WALLET_ZRO"
    node_id = "ff" * 32
    peer_key = "11" * 32

    handler, node = _make_handler(
        wallet_map={wallet: node_id},
        pubkey_map={node_id: peer_key},
    )

    event = {
        "event": "payment.finalized.solana",
        "chain_id": "solana-mainnet",
        "from_address": wallet,
        "amount": 0.0,
        "tx_hash": "txhash456",
        "tx_index": 0,
    }

    await handler(event)

    # No credit applied for zero amount
    node._enqueue_write.assert_not_called()
    node.bus.emit.assert_not_called()


@pytest.mark.asyncio
async def test_non_finalized_event_ignored():
    """Events not starting with payment.finalized. must be ignored."""
    handler, node = _make_handler()

    event = {
        "event": "payment.received.solana",  # NOT finalized
        "from_address": "WALLET",
        "amount": 10.0,
        "tx_hash": "tx",
    }

    await handler(event)

    node._enqueue_write.assert_not_called()
