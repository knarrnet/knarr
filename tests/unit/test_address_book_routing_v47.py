"""Contract test: explicit-tier address book routing priority (v0.47.0, Track C).

This test MUST FAIL against the v0.46.0 baseline (before implementation).
After implementation it MUST PASS.

Verifies that _select_execute_provider gives explicit-tier (operator-declared)
peers priority over generic DHT-discovered peers.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


def _make_server_with_candidates(providers, explicit_node_ids=None, routing_defaults=None):
    """Build a minimal CockpitServer with mocked dependencies."""
    from knarr.dashboard.server import CockpitServer

    node = MagicMock()
    node.node_info.node_id = "local-node-id-0000"
    node.node_info.port = 8080
    node._handlers = {}  # no local skill handlers

    # Skill list: one skill with the given providers
    node.get_skills.return_value = {
        "network": [{
            "name": "test-skill",
            "providers": list(providers),
        }]
    }

    # Address book: return rows for explicit-tier nodes
    def _get_addresses_by_tier(tier, limit=200):
        if tier != "explicit":
            return []
        return [{"node_id": nid} for nid in (explicit_node_ids or [])]

    node.storage = MagicMock()
    node.storage.get_addresses_by_tier.side_effect = _get_addresses_by_tier

    # Routing policy
    defaults = routing_defaults or {}
    routing_policy = {"defaults": {"local_weight": 1.0, **defaults}}

    server = CockpitServer.__new__(CockpitServer)
    server._node = node
    server._routing_policy = routing_policy
    server._exposures = {}
    server._rate_limits = {}
    server._config_dir = "."
    return server


class TestExplicitTierWinsOverDHT(unittest.TestCase):
    """Explicit-tier candidate must beat a generic DHT candidate."""

    def test_explicit_beats_remote(self):
        """Two candidates: one explicit, one DHT-only — explicit must win."""
        explicit_id = "explicit-node-aaaa"
        remote_id = "remote-node-bbbb"

        providers = [
            {"node_id": remote_id, "host": "10.0.0.1", "port": 9000},
            {"node_id": explicit_id, "host": "10.0.0.2", "port": 9001},
        ]

        server = _make_server_with_candidates(
            providers=providers,
            explicit_node_ids=[explicit_id],
        )

        # Run many times to rule out random selection bias
        winners = set()
        for _ in range(20):
            result = server._select_execute_provider("test-skill")
            if result:
                winners.add(result["node_id"])

        self.assertIn(explicit_id, winners, "Explicit-tier candidate must win at least once")
        self.assertNotIn(remote_id, winners, "DHT-only candidate must never beat explicit")

    def test_local_beats_explicit(self):
        """Three candidates: local, explicit, DHT — local must always win."""
        from knarr.dashboard.server import CockpitServer

        local_id = "local-node-id-0000"
        explicit_id = "explicit-node-cccc"
        remote_id = "remote-node-dddd"

        providers = [
            {"node_id": remote_id, "host": "10.0.0.1", "port": 9000},
            {"node_id": explicit_id, "host": "10.0.0.2", "port": 9001},
        ]

        node = MagicMock()
        node.node_info.node_id = local_id
        node.node_info.port = 8080
        node._handlers = {"test-skill": MagicMock()}  # local handler exists

        node.get_skills.return_value = {
            "network": [{
                "name": "test-skill",
                "providers": list(providers),
            }]
        }

        def _get_addresses_by_tier(tier, limit=200):
            if tier == "explicit":
                return [{"node_id": explicit_id}]
            return []

        node.storage = MagicMock()
        node.storage.get_addresses_by_tier.side_effect = _get_addresses_by_tier

        server = CockpitServer.__new__(CockpitServer)
        server._node = node
        server._routing_policy = {"defaults": {"local_weight": 1.0}}
        server._exposures = {}
        server._rate_limits = {}
        server._config_dir = "."

        winners = set()
        for _ in range(20):
            result = server._select_execute_provider("test-skill")
            if result:
                winners.add(result["node_id"])

        self.assertEqual(winners, {local_id}, "Local must always beat explicit and remote")

    def test_empty_address_book_no_crash(self):
        """Empty address book: both candidates are remote, no crash."""
        providers = [
            {"node_id": "node-a", "host": "10.0.0.1", "port": 9000},
            {"node_id": "node-b", "host": "10.0.0.2", "port": 9001},
        ]
        server = _make_server_with_candidates(
            providers=providers,
            explicit_node_ids=[],
        )
        result = server._select_execute_provider("test-skill")
        self.assertIsNotNone(result)
        self.assertIn(result["node_id"], {"node-a", "node-b"})

    def test_explicit_weight_config_respected(self):
        """explicit_weight=0.3, remote_weight=0.4 → remote beats explicit."""
        explicit_id = "explicit-node-eeee"
        remote_id = "remote-node-ffff"

        providers = [
            {"node_id": remote_id, "host": "10.0.0.1", "port": 9000},
            {"node_id": explicit_id, "host": "10.0.0.2", "port": 9001},
        ]

        server = _make_server_with_candidates(
            providers=providers,
            explicit_node_ids=[explicit_id],
            routing_defaults={"explicit_weight": 0.3, "remote_weight": 0.4},
        )

        winners = set()
        for _ in range(20):
            result = server._select_execute_provider("test-skill")
            if result:
                winners.add(result["node_id"])

        self.assertIn(remote_id, winners, "Remote must win when remote_weight > explicit_weight")
        self.assertNotIn(explicit_id, winners, "Explicit must lose when its weight is lower")

    def test_explicit_not_in_skills_not_injected(self):
        """Explicit-tier node not in get_skills() must not appear as candidate."""
        explicit_id = "explicit-only-node"
        remote_id = "dht-only-node"

        # get_skills() returns only the remote node, NOT the explicit-only node
        providers = [
            {"node_id": remote_id, "host": "10.0.0.1", "port": 9000},
        ]

        server = _make_server_with_candidates(
            providers=providers,
            explicit_node_ids=[explicit_id],  # in address book, but not in DHT skill list
        )

        results = set()
        for _ in range(10):
            result = server._select_execute_provider("test-skill")
            if result:
                results.add(result["node_id"])

        self.assertNotIn(explicit_id, results, "Explicit-only (no DHT announcement) must not be injected")
        self.assertIn(remote_id, results)

    def test_dht_only_no_address_book(self):
        """When there's no address book (storage missing), fall back gracefully."""
        from knarr.dashboard.server import CockpitServer

        node = MagicMock()
        node.node_info.node_id = "local-id"
        node.node_info.port = 8080
        node._handlers = {}
        node.get_skills.return_value = {
            "network": [{"name": "test-skill", "providers": [
                {"node_id": "node-x", "host": "10.0.0.1", "port": 9000},
            ]}]
        }
        # No storage attribute at all
        del node.storage

        server = CockpitServer.__new__(CockpitServer)
        server._node = node
        server._routing_policy = {"defaults": {"local_weight": 1.0}}
        server._exposures = {}
        server._rate_limits = {}
        server._config_dir = "."

        result = server._select_execute_provider("test-skill")
        self.assertIsNotNone(result)
