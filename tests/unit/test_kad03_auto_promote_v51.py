"""KAD-03: Auto-promote passive → full mode tests."""
import sys
import os
import asyncio
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'plugins', '00-kademlia'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from unittest.mock import MagicMock, AsyncMock


def _make_ctx(node_id=None):
    ctx = MagicMock()
    ctx.node_id = node_id or "a" * 64
    ctx.get_peers = MagicMock(return_value=[])
    ctx.send_fire_forget = AsyncMock()
    ctx.log = MagicMock()
    return ctx


def _make_health(uptime=120.0, peer_count=10):
    from knarr.dht.plugins import NodeHealth
    return NodeHealth(
        event_loop_lag_ms=0.0,
        active_connections=0,
        max_connections=100,
        write_queue_depth=0,
        peer_count=peer_count,
        uptime_seconds=uptime,
    )


def _make_plugin(mode="passive", min_uptime=60.0, min_peers=5):
    ctx = _make_ctx()
    config = {
        "mode": mode,
        "k": 4,
        "debug": False,
        "auto_promote_min_uptime_seconds": min_uptime,
        "auto_promote_min_peers": min_peers,
    }
    from handler import KademliaPlugin
    return KademliaPlugin(ctx, config), ctx


def test_auto_promotes_after_60s_and_5_peers():
    """Node must auto-promote from passive to full after 60s uptime + 5 peers."""
    plugin, ctx = _make_plugin(mode="passive")
    assert plugin.mode == "passive"
    assert plugin._lookup is None

    asyncio.run(plugin.on_tick([], _make_health(uptime=60.0, peer_count=5)))

    assert plugin.mode == "full", "Should have promoted to full"
    assert plugin._lookup is not None, "Lookup module must be initialized after promotion"


def test_does_not_promote_before_60s():
    """Node must NOT promote when uptime < 60s."""
    plugin, ctx = _make_plugin(mode="passive", min_uptime=60.0, min_peers=5)

    asyncio.run(plugin.on_tick([], _make_health(uptime=30.0, peer_count=10)))

    assert plugin.mode == "passive", "Must not promote with uptime < threshold"


def test_does_not_promote_with_fewer_than_5_peers():
    """Node must NOT promote with fewer than 5 peers."""
    plugin, ctx = _make_plugin(mode="passive", min_uptime=60.0, min_peers=5)

    asyncio.run(plugin.on_tick([], _make_health(uptime=120.0, peer_count=4)))

    assert plugin.mode == "passive", "Must not promote with peer_count < threshold"


def test_passive_locked_never_auto_promotes():
    """passive_locked mode must never auto-promote even with sufficient uptime + peers."""
    plugin, ctx = _make_plugin(mode="passive_locked")
    assert plugin.mode == "passive_locked"

    asyncio.run(plugin.on_tick([], _make_health(uptime=3600.0, peer_count=100)))

    assert plugin.mode == "passive_locked", "passive_locked must never promote"
    assert plugin._lookup is None


def test_already_full_mode_no_change():
    """A node already in full mode must remain full after tick."""
    plugin, ctx = _make_plugin(mode="full")
    assert plugin.mode == "full"

    asyncio.run(plugin.on_tick([], _make_health(uptime=120.0, peer_count=10)))

    assert plugin.mode == "full"


def test_auto_promote_exactly_at_threshold():
    """Auto-promote must trigger at exactly the threshold values."""
    plugin, ctx = _make_plugin(mode="passive", min_uptime=60.0, min_peers=5)

    asyncio.run(plugin.on_tick([], _make_health(uptime=60.0, peer_count=5)))

    assert plugin.mode == "full", "Must promote at exactly threshold"
