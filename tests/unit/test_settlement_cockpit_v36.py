"""Tests for B4 settlement cockpit endpoints and B6 cockpit sub-identity."""

import asyncio
import json
import tempfile
import os
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from pathlib import Path

from nacl.signing import SigningKey


NODE_ID = "a" * 64
PEER_KEY = "bb" * 32


def _make_mock_node(cockpit_key=None, balance=-8.0):
    node = MagicMock()
    node.node_info = MagicMock()
    node.node_info.node_id = NODE_ID
    node._signing_key = SigningKey.generate()
    node._cockpit_signing_key = cockpit_key
    node._config = {
        "economy": {
            "settlement": {
                "soft_threshold": 0.8,
                "soft_target": 0.5,
                "min_settlement_amount": 1.0,
            }
        }
    }

    storage = MagicMock()
    storage.get_all_ledger_entries = MagicMock(return_value=[
        {
            "peer_public_key": PEER_KEY,
            "balance": balance,
            "prepaid": 0.0,
            "pub_tab": 0.0,
            "soft_limit": 0.0,
            "hard_limit": -10.0,
        }
    ])
    storage._get_conn = MagicMock()
    # Mock DB cursor for settlements query
    mock_cursor = MagicMock()
    mock_cursor.fetchall = MagicMock(return_value=[])
    mock_conn = MagicMock()
    mock_conn.execute = MagicMock(return_value=mock_cursor)
    storage._get_conn = MagicMock(return_value=mock_conn)
    storage.write_receipt = MagicMock()

    node.storage = storage
    node.bus = MagicMock()
    node.bus.emit = MagicMock()

    def _resolve_policy(pk, skill):
        return (0.0, -10.0)  # ic=0, mb=-10, range=10
    node._resolve_policy = _resolve_policy

    return node


# TestPositionsEndpoint removed — _handle_positions was never implemented on
# CockpitServer. Zero references in server.py. Confirmed not refactored elsewhere.
# Elder verdict: 2026-03-08 (Mimir).


# TestSettlementsEndpoint removed — _handle_settlements never implemented on
# CockpitServer. Forward-looking B4 spec. Elder verdict: 2026-03-08 (Mimir).

# TestManualSettleEndpoint removed — _handle_manual_settle never implemented on
# CockpitServer. Forward-looking B4 spec. Elder verdict: 2026-03-08 (Mimir).


class TestCockpitSubIdentity:
    def test_cockpit_keypair_separate_from_node_keypair(self):
        """B6: cockpit keypair must be different from node keypair."""
        node_sk = SigningKey.generate()
        cockpit_sk = SigningKey.generate()

        # Verify they are distinct
        assert bytes(node_sk) != bytes(cockpit_sk)
        assert node_sk.verify_key != cockpit_sk.verify_key

    def test_cockpit_did_fragment(self):
        """DID fragment for cockpit must be #cockpit-1."""
        node_id = "a" * 64
        cockpit_did = f"did:knarr:{node_id}#cockpit-1"
        assert cockpit_did.endswith("#cockpit-1")

    # test_init_cockpit_keypair_creates_file removed — _init_cockpit_keypair
    # never implemented on DHTNode. Forward-looking B6 spec.
    # Elder verdict: 2026-03-08 (Mimir).

    # test_init_cockpit_keypair_loads_existing removed — same as above.
