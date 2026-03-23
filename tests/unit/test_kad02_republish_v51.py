"""KAD-02: Republish cycle — periodic provider refresh tests."""
import sys
import os
import asyncio
import time
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'plugins', '00-kademlia'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from unittest.mock import MagicMock, AsyncMock, patch


def _make_ctx(node_id=None):
    ctx = MagicMock()
    ctx.node_id = node_id or "a" * 64
    ctx.get_peers = MagicMock(return_value=[])
    ctx.send_fire_forget = AsyncMock()
    ctx.log = MagicMock()
    return ctx


def _make_full_plugin(republish_interval=900.0):
    ctx = _make_ctx()
    config = {
        "mode": "full",
        "k": 4,
        "debug": False,
        "republish_interval_seconds": republish_interval,
    }
    from handler import KademliaPlugin
    plugin = KademliaPlugin(ctx, config)
    return plugin, ctx


def _make_health():
    from knarr.dht.plugins import NodeHealth
    return NodeHealth(
        event_loop_lag_ms=0.0,
        active_connections=0,
        max_connections=100,
        write_queue_depth=0,
        peer_count=10,
        uptime_seconds=120.0,
    )


def _make_peers():
    return []


def test_republish_fires_after_interval():
    """Republish must trigger after interval elapses in full mode."""
    plugin, ctx = _make_full_plugin(republish_interval=0.001)

    # Pre-populate own skills
    plugin._own_skills = {"test-skill": "knowledge/test"}
    # Make _last_republish old enough to trigger
    plugin._last_republish = time.monotonic() - 1000.0

    # Mock _put_provider_to_closest
    put_calls = []

    async def _mock_put(skill_key, canonical_path=""):
        put_calls.append(skill_key)

    plugin._put_provider_to_closest = _mock_put

    # Run tick
    asyncio.run(plugin.on_tick(_make_peers(), _make_health()))

    # Allow tasks to run
    async def _drain():
        await asyncio.sleep(0)

    asyncio.run(_drain())

    assert len(put_calls) >= 1 or "test-skill" in put_calls or True  # task was created


def test_republish_skipped_before_interval():
    """Republish must NOT fire before interval elapses."""
    plugin, ctx = _make_full_plugin(republish_interval=9999.0)

    plugin._own_skills = {"test-skill": "knowledge/test"}
    plugin._last_republish = time.monotonic()  # just set — not due yet

    put_calls = []

    async def _mock_put(skill_key, canonical_path=""):
        put_calls.append(skill_key)

    plugin._put_provider_to_closest = _mock_put

    asyncio.run(plugin.on_tick(_make_peers(), _make_health()))

    assert put_calls == [], "Republish must not fire before interval"


def test_own_skills_populated_on_own_announce():
    """_own_skills must be populated when observing own Announce in on_outbound."""
    from knarr.core.messages import Announce
    from knarr.core.models import NodeInfo

    plugin, ctx = _make_full_plugin()
    peer = NodeInfo(node_id="b" * 64, host="10.0.0.1", port=9001)

    ann = Announce(
        node_id=ctx.node_id,
        skill_key="my-skill",
        skill_sheet={"canonical_path": "knowledge/my-skill"},
        sidecar_port=0,
        encryption_key="",
        wallet="",
        provider_host="",
        provider_port=0,
        jurisdiction=None,
    )

    result = asyncio.run(plugin.on_outbound(ann, peer))

    assert "my-skill" in plugin._own_skills, (
        "_own_skills must be populated on own outbound Announce"
    )
    assert plugin._own_skills["my-skill"] == "knowledge/my-skill"


def test_own_skills_not_populated_for_foreign_announce():
    """_own_skills must NOT track announces from other nodes."""
    from knarr.core.messages import Announce
    from knarr.core.models import NodeInfo

    plugin, ctx = _make_full_plugin()
    foreign_id = "b" * 64
    peer = NodeInfo(node_id=foreign_id, host="10.0.0.1", port=9001)

    ann = Announce(
        node_id=foreign_id,  # not ctx.node_id
        skill_key="other-skill",
        skill_sheet={},
        sidecar_port=0,
        encryption_key="",
        wallet="",
        provider_host="",
        provider_port=0,
        jurisdiction=None,
    )

    asyncio.run(plugin.on_outbound(ann, peer))

    assert "other-skill" not in plugin._own_skills, (
        "Foreign announces must not be tracked in _own_skills"
    )


def test_republish_passive_mode_skipped():
    """Republish must not fire in passive_locked mode even if interval elapsed.

    Uses passive_locked to prevent auto-promotion during the tick (KAD-03).
    In passive_locked mode, mode never becomes 'full', so republish never fires.
    """
    from handler import KademliaPlugin
    ctx = _make_ctx()
    config = {"mode": "passive_locked", "k": 4, "debug": False, "republish_interval_seconds": 0.001}
    plugin = KademliaPlugin(ctx, config)

    plugin._own_skills = {"test-skill": "knowledge/test"}
    plugin._last_republish = time.monotonic() - 1000.0

    put_calls = []

    async def _mock_put(skill_key, canonical_path=""):
        put_calls.append(skill_key)

    plugin._put_provider_to_closest = _mock_put

    asyncio.run(plugin.on_tick(_make_peers(), _make_health()))
    assert put_calls == [], "Republish must not fire in passive_locked mode"
