"""Tests for B3 counterparty acceptance and reconciliation (handlers.py)."""

import asyncio
import json
import hashlib
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from nacl.signing import SigningKey

from knarr.core.proof import sign_document


NODE_ID = "a" * 64

# Peer keys derived consistently: PEER_SK → PEER_PK → PEER_NODE_ID
# This must be module-level to be stable, but we use a fixed seed via bytes.
_PEER_SK_BYTES = bytes(range(32))  # deterministic 32-byte seed
PEER_SK = SigningKey(_PEER_SK_BYTES)
PEER_PK = PEER_SK.verify_key.encode().hex()
PEER_NODE_ID = hashlib.sha256(bytes.fromhex(PEER_PK)).hexdigest()


def _make_node(signing_key=None, balance=-8.0, peer_pk=None):
    """Create a minimal mock node for handler testing."""
    if peer_pk is None:
        peer_pk = PEER_PK

    node = MagicMock()
    node.node_info = MagicMock()
    node.node_info.node_id = NODE_ID

    if signing_key is None:
        signing_key = SigningKey.generate()
    node._signing_key = signing_key

    node._config = {
        "economy": {
            "settlement": {
                "reconciliation_tolerance": 0.05,
                "reconciliation_timeout": 60,
            }
        }
    }

    # Storage
    storage = MagicMock()
    storage.get_all_ledger_entries = MagicMock(return_value=[
        {
            "peer_public_key": peer_pk,
            "balance": balance,
            "prepaid": 0.0,
            "pub_tab": 0.0,
            "soft_limit": 0.0,
            "hard_limit": -10.0,
        }
    ])
    storage.get_ledger_balance = MagicMock(return_value=balance)
    storage.write_receipt = MagicMock()
    storage.get_receipt = MagicMock(return_value=None)  # v0.36.0: dedup check needs this
    storage.get_receipts_by_type = MagicMock(return_value=[])  # v0.36.0: confirmation dedup
    storage.update_ledger_refund = AsyncMock(return_value=None)
    storage.queue_settlement = MagicMock()
    node.storage = storage

    # Bus
    node.bus = MagicMock()
    node.bus.emit = MagicMock()

    # Plugins
    node._plugins = MagicMock()
    node._plugins.on_inbound_settlement = AsyncMock(return_value=True)

    # Sync
    node._sync = MagicMock()
    node._sync.enqueue = AsyncMock()

    # Enqueue write
    async def _enqueue_write(op, *args, **kwargs):
        return op(*args, **kwargs)
    node._enqueue_write = _enqueue_write

    return node


def _make_settle_body(signing_key, proposer_node_id, amount=50.0, peer_pk=None,
                      authority_key=None):
    """Create a minimal settle_request mail body with dual signatures.

    signing_key: the proposer's SigningKey (must match what's in the ledger)
    proposer_node_id: SHA-256(verify_key) of the signing_key
    authority_key: optional separate authority key; defaults to signing_key
    """
    if peer_pk is None:
        peer_pk = PEER_PK
    if authority_key is None:
        authority_key = signing_key

    payload = {
        "document_type": "settlement_prepared",
        "proposer": proposer_node_id,
        "counterparty": NODE_ID,
        "amount": amount,
        "formula": "test",
        "proposer_balance": -8.0,
        "counterparty_balance_claimed": 8.0,
        "utilization": 0.85,
        "target_utilization": 0.5,
        "receipt_id": "sp_test01",
        "timestamp": "2026-01-01T00:00:00.000Z",
        "version": 2,
    }
    signed_doc = sign_document(payload, signing_key, f"did:knarr:{proposer_node_id}#key-1")

    # Build authority proof (second signature for dual-sig settlement)
    authority_proof_doc = sign_document(
        payload, authority_key, f"did:knarr:{proposer_node_id}#cockpit-1"
    )

    return {
        "type": "knarr/commerce/settle_request",
        "document": signed_doc,
        "authority_proof": authority_proof_doc["proof"],
        "amount": amount,
        "peer_key": peer_pk,
        "accepted_receipt_id": "sa_test",
        "schema_version": "1.0",
        "timestamp": 0.0,
        # Legacy fields for backward compat
        "current_balance": -8.0,
        "credit_limit": 10.0,
        "tx_hash": "none",
        "amount_settled": amount,
    }


class TestHandleSettleRequestBasic:
    def test_handler_accepts_valid_request(self):
        """Plugin returns True → handler processes the request."""
        from knarr.commerce.handlers import make_commerce_handlers

        # Use the stable peer SK — ledger has matching PEER_PK
        node = _make_node()

        body = _make_settle_body(PEER_SK, PEER_NODE_ID, amount=8.0)
        item = {"from_node": PEER_NODE_ID, "body": body}

        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/settle_request"]

        asyncio.get_event_loop().run_until_complete(handler(item))

        # Plugin hook was called (after signature validation passed)
        node._plugins.on_inbound_settlement.assert_called_once()

    def test_handler_rejects_when_plugin_rejects(self):
        """Plugin returns False → no confirmation sent."""
        from knarr.commerce.handlers import make_commerce_handlers

        node = _make_node()
        node._plugins.on_inbound_settlement = AsyncMock(return_value=False)

        body = _make_settle_body(PEER_SK, PEER_NODE_ID, amount=8.0)
        item = {"from_node": PEER_NODE_ID, "body": body}

        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/settle_request"]

        asyncio.get_event_loop().run_until_complete(handler(item))

        # Confirmation should NOT be sent
        node._sync.enqueue.assert_not_called()

    def test_none_body_returns_early(self):
        """Malformed body (None) should return without action."""
        from knarr.commerce.handlers import make_commerce_handlers

        node = _make_node()
        item = {"from_node": PEER_NODE_ID, "body": None}

        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/settle_request"]

        asyncio.get_event_loop().run_until_complete(handler(item))

        node._sync.enqueue.assert_not_called()
        node.storage.queue_settlement.assert_not_called()


class TestSettlementHandlerDualSig:
    def test_valid_node_sig_proceeds(self):
        """A properly signed document with a resolvable key should proceed."""
        from knarr.commerce.handlers import make_commerce_handlers

        # Use stable PEER_SK — its verify key matches PEER_PK in the ledger
        node = _make_node()

        body = _make_settle_body(PEER_SK, PEER_NODE_ID, amount=8.0)
        item = {"from_node": PEER_NODE_ID, "body": body}

        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/settle_request"]

        asyncio.get_event_loop().run_until_complete(handler(item))

        # Plugin hook was called — sig validation passed
        node._plugins.on_inbound_settlement.assert_called_once()

    def test_unknown_proposer_drops_silently(self):
        """If proposer can't be resolved, handler drops with no action."""
        from knarr.commerce.handlers import make_commerce_handlers

        node = _make_node()
        # Clear ledger so key can't be resolved
        node.storage.get_all_ledger_entries = MagicMock(return_value=[])

        body = _make_settle_body(PEER_SK, PEER_NODE_ID, amount=8.0)
        item = {"from_node": PEER_NODE_ID, "body": body}

        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/settle_request"]

        asyncio.get_event_loop().run_until_complete(handler(item))

        # No plugin call — dropped at key-resolution step
        node._plugins.on_inbound_settlement.assert_not_called()


class TestReconciliationTolerance:
    def test_within_tolerance_proceeds(self):
        """Small divergence within tolerance should proceed to acceptance."""
        from knarr.commerce.handlers import make_commerce_handlers

        node = _make_node(balance=-8.0)  # our balance is -8
        # Claim is 8.0 (divergence is 0% since |8 - 8| / 8 = 0)
        body = _make_settle_body(PEER_SK, PEER_NODE_ID, amount=8.0)
        item = {"from_node": PEER_NODE_ID, "body": body}

        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/settle_request"]

        asyncio.get_event_loop().run_until_complete(handler(item))

        # Handler ran to completion without early exit due to divergence
        node._plugins.on_inbound_settlement.assert_called_once()
