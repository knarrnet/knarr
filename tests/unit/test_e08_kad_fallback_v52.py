"""E-08: _select_execute_provider KAD fallback.

When gossip catalog returns empty candidates, _select_execute_provider
should query KAD via storage.query_skills_by_name() as fallback.

Dynamic skills (e.g. casino `game-seat-{id}`) that missed gossip propagation
are invisible without this fallback.
"""

import pytest
from unittest.mock import MagicMock, patch


def make_server_with_skills(gossip_skills=None, kad_skills=None):
    """Create a minimal CockpitServer mock for testing _select_execute_provider."""
    from knarr.dashboard.server import CockpitServer

    mock_node = MagicMock()
    mock_node.node_info.node_id = "a" * 64
    mock_node.node_info.port = 9000
    mock_node._handlers = {}

    # Gossip catalog via get_skills()
    gossip_skills = gossip_skills or []
    mock_node.get_skills.return_value = {"network": gossip_skills, "local": []}

    # KAD storage
    mock_storage = MagicMock()
    kad_skills = kad_skills or []
    mock_storage.query_skills_by_name.return_value = kad_skills
    mock_storage.get_addresses_by_tier.return_value = []
    mock_node.storage = mock_storage

    server = CockpitServer.__new__(CockpitServer)
    server._node = mock_node
    server._routing_policy = {"defaults": {"local_weight": 1.0, "explicit_weight": 0.8, "remote_weight": 0.5}}

    return server


# ──────────────────────────────────────────────────────────────────────────────
# E-08-A: Gossip has candidates — KAD not queried
# ──────────────────────────────────────────────────────────────────────────────

def test_gossip_hit_kad_not_queried():
    """When gossip finds candidates, KAD query_skills_by_name should not be called."""
    gossip_skills = [{
        "name": "game-skill",
        "providers": [{"node_id": "b" * 64, "host": "1.2.3.4", "port": 9001}],
    }]
    server = make_server_with_skills(gossip_skills=gossip_skills)

    result = server._select_execute_provider("game-skill")
    # Should find a provider
    assert result is not None
    # KAD should NOT have been queried
    server._node.storage.query_skills_by_name.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# E-08-B: Gossip miss — KAD fallback is queried
# ──────────────────────────────────────────────────────────────────────────────

def test_gossip_miss_triggers_kad_fallback():
    """When gossip returns empty, KAD fallback query_skills_by_name is called."""
    kad_skills = [{"node_id": "c" * 64, "host": "2.3.4.5", "port": 9002,
                   "skill_sheet": {"name": "game-seat-42"}, "_last_seen": 0,
                   "_load": -1, "_provider_public_key": "", "sidecar_port": 0}]
    server = make_server_with_skills(gossip_skills=[], kad_skills=kad_skills)

    result = server._select_execute_provider("game-seat-42")

    # KAD MUST have been queried
    server._node.storage.query_skills_by_name.assert_called_once_with("game-seat-42")
    # Should return the KAD-found provider
    assert result is not None
    assert result["node_id"] == "c" * 64


# ──────────────────────────────────────────────────────────────────────────────
# E-08-C: Gossip miss, KAD also empty — returns None
# ──────────────────────────────────────────────────────────────────────────────

def test_both_miss_returns_none():
    """When both gossip and KAD miss, _select_execute_provider returns None."""
    server = make_server_with_skills(gossip_skills=[], kad_skills=[])

    result = server._select_execute_provider("unknown-dynamic-skill")
    assert result is None
    server._node.storage.query_skills_by_name.assert_called_once()


# ──────────────────────────────────────────────────────────────────────────────
# E-08-D: KAD results tagged with _kad_fallback flag
# ──────────────────────────────────────────────────────────────────────────────

def test_kad_result_tagged_with_fallback_flag():
    """Providers found via KAD fallback should have _kad_fallback=True."""
    kad_skills = [{"node_id": "d" * 64, "host": "3.4.5.6", "port": 9003,
                   "skill_sheet": {}, "_last_seen": 0, "_load": -1,
                   "_provider_public_key": "", "sidecar_port": 0}]
    server = make_server_with_skills(kad_skills=kad_skills)

    result = server._select_execute_provider("dynamic-skill")
    if result:
        assert result.get("_kad_fallback") is True


# ──────────────────────────────────────────────────────────────────────────────
# E-08-E: knarr-mail routing not affected by fallback
# ──────────────────────────────────────────────────────────────────────────────

def test_knarr_mail_unaffected_by_fallback():
    """knarr-mail must always route local regardless of fallback."""
    kad_skills = [{"node_id": "e" * 64, "host": "4.5.6.7", "port": 9004,
                   "skill_sheet": {}, "_last_seen": 0, "_load": -1,
                   "_provider_public_key": "", "sidecar_port": 0}]
    server = make_server_with_skills(kad_skills=kad_skills)
    # No local handler for knarr-mail
    server._node._handlers = {}

    result = server._select_execute_provider("knarr-mail")
    # knarr-mail with no local handler → None, not KAD result
    assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# E-08-F: Storage error in KAD query is handled gracefully
# ──────────────────────────────────────────────────────────────────────────────

def test_kad_storage_error_handled():
    """Storage error during KAD fallback should not raise — return None."""
    server = make_server_with_skills()
    server._node.storage.query_skills_by_name.side_effect = Exception("DB error")

    # Should not raise
    result = server._select_execute_provider("any-skill")
    assert result is None
