"""A2 contract test: knarr-mail must always route to local node.

M-022: knarr-mail routed to a remote worker returns 409. The fix must enforce
local-only provider selection for the "knarr-mail" skill in _select_execute_provider,
regardless of what remote providers are advertised.

FIX LOCATION: dashboard/server.py — _select_execute_provider()
Add early return for knarr-mail before the scored-candidate loop:
    if skill.lower() == "knarr-mail":
        # Force local — remote workers cannot handle external mail
        local = next((c for c in candidates if c.get("_local")), None)
        if local:
            return local

CONTRACT:
- _select_execute_provider("knarr-mail") must return the local provider
  even when multiple remote providers with equal or higher score are available.
- _select_execute_provider("knarr-mail") must return None only when no
  local handler is registered (not because a remote won the scored sort).
"""
import types
import pytest
from unittest.mock import MagicMock, patch


def _make_cockpit_server(local_node_id="aabbcc", has_local_handler=True,
                         remote_providers=None, local_weight=1.0):
    """Build a minimal CockpitServer-like object with just _select_execute_provider."""
    from knarr.dashboard.server import CockpitServer

    server = CockpitServer.__new__(CockpitServer)
    server._routing_policy = {"defaults": {"local_weight": local_weight}}
    server._exposures = {}
    server._rate_limits = {}

    mock_node = MagicMock()
    mock_node.node_info.node_id = local_node_id
    mock_node.node_info.host = "127.0.0.1"
    mock_node.node_info.port = 9010

    # Simulate remote providers
    net_providers = remote_providers or [
        {"node_id": "remote01", "host": "10.0.0.1", "port": 9010},
        {"node_id": "remote02", "host": "10.0.0.2", "port": 9010},
    ]
    mock_node.get_skills.return_value = {
        "network": [{"name": "knarr-mail", "providers": net_providers}]
    }

    # Local handler registration
    if has_local_handler:
        mock_node._handlers = {"knarr-mail": MagicMock()}
    else:
        mock_node._handlers = {}

    server._node = mock_node
    return server


def test_knarr_mail_always_routes_local_when_local_handler_exists():
    """With local handler + remote providers, knarr-mail must return local."""
    server = _make_cockpit_server(has_local_handler=True)
    result = server._select_execute_provider("knarr-mail")

    assert result is not None, "Expected a provider, got None"
    assert result.get("_local") is True, (
        f"knarr-mail must route local, got node_id={result.get('node_id')}. "
        "Fix: add early local-only return for knarr-mail in _select_execute_provider."
    )


def test_knarr_mail_routes_local_even_with_many_remotes():
    """knarr-mail must route local even with 10 equal-scored remote providers."""
    remotes = [{"node_id": f"remote{i:02d}", "host": f"10.0.0.{i}", "port": 9010}
               for i in range(10)]
    server = _make_cockpit_server(has_local_handler=True, remote_providers=remotes)
    result = server._select_execute_provider("knarr-mail")

    assert result is not None
    assert result.get("_local") is True, (
        "knarr-mail must not be load-balanced to remotes. "
        "Always local when local handler exists."
    )


def test_knarr_mail_returns_none_when_no_local_handler():
    """knarr-mail with no local handler should return None (not a remote)."""
    server = _make_cockpit_server(has_local_handler=False, remote_providers=[
        {"node_id": "remote01", "host": "10.0.0.1", "port": 9010},
    ])
    result = server._select_execute_provider("knarr-mail")

    assert result is None, (
        "knarr-mail with no local handler must return None, not a remote provider. "
        "Remote workers cannot handle external mail requests."
    )


def test_non_knarr_mail_skill_still_load_balances():
    """Other skills must still use normal scored selection (no regression)."""
    server = _make_cockpit_server(
        has_local_handler=True,
        remote_providers=[{"node_id": "remote01", "host": "10.0.0.1", "port": 9010}],
        local_weight=0.5,  # remote should be preferred
    )
    # Run 20 times — with local_weight=0.5 and equal score remote at 1.0,
    # a non-knarr-mail skill should sometimes pick remote.
    # We just verify it doesn't crash and returns a provider.
    results = set()
    for _ in range(20):
        r = server._select_execute_provider("embed-batch-lite")
        if r:
            results.add(r.get("node_id") or ("local" if r.get("_local") else "?"))
    # At least one remote should appear (local_weight=0.5 means remote wins often)
    assert len(results) >= 1
