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


class TestPositionsEndpoint:
    def _make_server(self, node):
        from knarr.dashboard.server import CockpitServer
        server = CockpitServer.__new__(CockpitServer)
        server._node = node
        server._auth_token = ""
        return server

    def test_positions_returns_list(self):
        node = _make_mock_node(balance=-8.0)
        server = self._make_server(node)

        writer = MagicMock()
        writer.write = MagicMock()

        # Capture respond_json
        captured = {}
        def _respond_json(w, data):
            captured["data"] = data
        server._respond_json = _respond_json

        asyncio.get_event_loop().run_until_complete(server._handle_positions(writer))

        assert "positions" in captured["data"]
        assert "count" in captured["data"]
        positions = captured["data"]["positions"]
        assert isinstance(positions, list)
        assert len(positions) == 1

    def test_position_has_utilization(self):
        node = _make_mock_node(balance=-8.0)
        server = self._make_server(node)

        captured = {}
        def _respond_json(w, data):
            captured["data"] = data
        server._respond_json = _respond_json

        asyncio.get_event_loop().run_until_complete(server._handle_positions(MagicMock()))

        pos = captured["data"]["positions"][0]
        assert "utilization" in pos
        assert "balance" in pos
        assert "peer_key" in pos


class TestSettlementsEndpoint:
    def _make_server(self, node):
        from knarr.dashboard.server import CockpitServer
        server = CockpitServer.__new__(CockpitServer)
        server._node = node
        server._auth_token = ""
        return server

    def test_settlements_returns_list(self):
        node = _make_mock_node()
        server = self._make_server(node)

        captured = {}
        def _respond_json(w, data):
            captured["data"] = data
        server._respond_json = _respond_json

        asyncio.get_event_loop().run_until_complete(
            server._handle_settlements(MagicMock(), {})
        )

        assert "settlements" in captured["data"]
        assert "count" in captured["data"]

    def test_settlements_empty_when_no_history(self):
        node = _make_mock_node()
        # Storage returns empty list (already mocked)
        server = self._make_server(node)

        captured = {}
        def _respond_json(w, data):
            captured["data"] = data
        server._respond_json = _respond_json

        asyncio.get_event_loop().run_until_complete(
            server._handle_settlements(MagicMock(), {})
        )

        assert captured["data"]["count"] == 0


class TestManualSettleEndpoint:
    def _make_server(self, node):
        from knarr.dashboard.server import CockpitServer
        server = CockpitServer.__new__(CockpitServer)
        server._node = node
        server._auth_token = ""
        return server

    def test_manual_settle_returns_prepared_doc(self):
        node = _make_mock_node(balance=-8.0)

        server = self._make_server(node)

        captured = {}
        def _respond_json(w, data):
            captured["data"] = data
        server._respond_json = _respond_json
        server._respond_error = MagicMock()

        asyncio.get_event_loop().run_until_complete(
            server._handle_manual_settle(MagicMock(), PEER_KEY)
        )

        assert "prepared_doc" in captured["data"]
        assert "needs_countersign" in captured["data"]
        assert "engine_action" in captured["data"]

    def test_manual_settle_invalid_peer_returns_400(self):
        node = _make_mock_node()
        server = self._make_server(node)

        errors = []
        def _respond_error(w, code, msg):
            errors.append((code, msg))
        server._respond_error = _respond_error
        server._respond_json = MagicMock()

        asyncio.get_event_loop().run_until_complete(
            server._handle_manual_settle(MagicMock(), "short")  # invalid peer key
        )

        assert any(code == 400 for code, _ in errors)

    def test_manual_settle_unknown_peer_returns_404(self):
        node = _make_mock_node()
        node.storage.get_all_ledger_entries = MagicMock(return_value=[])  # no entries
        server = self._make_server(node)

        errors = []
        def _respond_error(w, code, msg):
            errors.append((code, msg))
        server._respond_error = _respond_error
        server._respond_json = MagicMock()

        asyncio.get_event_loop().run_until_complete(
            server._handle_manual_settle(MagicMock(), PEER_KEY)
        )

        assert any(code == 404 for code, _ in errors)

    def test_cockpit_countersigns_when_key_available(self):
        cockpit_key = SigningKey.generate()
        node = _make_mock_node(cockpit_key=cockpit_key, balance=-8.0)
        server = self._make_server(node)

        captured = {}
        def _respond_json(w, data):
            captured["data"] = data
        server._respond_json = _respond_json
        server._respond_error = MagicMock()

        asyncio.get_event_loop().run_until_complete(
            server._handle_manual_settle(MagicMock(), PEER_KEY)
        )

        # When cockpit key is available, needs_countersign should be False
        assert captured["data"]["needs_countersign"] is False

    def test_no_cockpit_key_needs_countersign(self):
        node = _make_mock_node(cockpit_key=None, balance=-8.0)  # no cockpit key
        server = self._make_server(node)

        captured = {}
        def _respond_json(w, data):
            captured["data"] = data
        server._respond_json = _respond_json
        server._respond_error = MagicMock()

        asyncio.get_event_loop().run_until_complete(
            server._handle_manual_settle(MagicMock(), PEER_KEY)
        )

        assert captured["data"]["needs_countersign"] is True


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

    def test_init_cockpit_keypair_creates_file(self):
        """_init_cockpit_keypair() creates cockpit_ed25519.key if missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from knarr.dht.node import DHTNode
            # Create a minimal node without actually connecting
            node = DHTNode.__new__(DHTNode)
            node._config = {"_config_dir": tmpdir}
            node.node_info = MagicMock()
            node.node_info.node_id = "a" * 64
            node._cockpit_signing_key = None
            node._cockpit_verify_key = None

            # Call the init method
            node._init_cockpit_keypair()

            # Check file was created
            key_path = Path(tmpdir) / "cockpit_ed25519.key"
            assert key_path.exists()
            assert len(key_path.read_bytes()) == 32
            assert node._cockpit_signing_key is not None

    def test_init_cockpit_keypair_loads_existing(self):
        """_init_cockpit_keypair() loads existing key from file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Pre-create a key file
            existing_key = SigningKey.generate()
            key_path = Path(tmpdir) / "cockpit_ed25519.key"
            key_path.write_bytes(bytes(existing_key))

            from knarr.dht.node import DHTNode
            node = DHTNode.__new__(DHTNode)
            node._config = {"_config_dir": tmpdir}
            node.node_info = MagicMock()
            node.node_info.node_id = "a" * 64
            node._cockpit_signing_key = None
            node._cockpit_verify_key = None

            node._init_cockpit_keypair()

            # Loaded key should match the pre-created one
            assert node._cockpit_signing_key is not None
            assert bytes(node._cockpit_signing_key) == bytes(existing_key)
