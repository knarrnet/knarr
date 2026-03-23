"""KAD-14: on_query fan-out rate limiting."""
import sys
import os
import asyncio
import time
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
    ctx.sign_bytes = None
    return ctx


def _make_plugin(max_lookups=5, mode="full"):
    ctx = _make_ctx()
    config = {
        "mode": mode,
        "k": 4,
        "debug": False,
        "max_lookups_per_minute": max_lookups,
        "disjoint_lookup_paths": 1,  # disable disjoint for rate limit tests
    }
    from handler import KademliaPlugin
    plugin = KademliaPlugin(ctx, config)
    return plugin, ctx


def test_lookups_within_limit_execute_normally():
    """Lookups within the rate limit must trigger a network lookup."""
    plugin, ctx = _make_plugin(max_lookups=3)

    lookup_calls = []

    async def _mock_find_providers(value):
        lookup_calls.append(value)
        return []

    plugin._lookup.find_providers = _mock_find_providers

    # Run 3 cache-miss queries (at the limit)
    async def _run():
        for i in range(3):
            await plugin.on_query("name", f"skill-{i}")

    asyncio.run(_run())

    assert len(lookup_calls) == 3, f"Expected 3 lookups, got {len(lookup_calls)}"


def test_lookups_beyond_limit_return_empty_without_fanout():
    """Lookups beyond the limit must return empty without triggering network lookups."""
    plugin, ctx = _make_plugin(max_lookups=2)

    lookup_calls = []

    async def _mock_find_providers(value):
        lookup_calls.append(value)
        return []

    plugin._lookup.find_providers = _mock_find_providers

    async def _run():
        results = []
        for i in range(5):
            r = await plugin.on_query("name", f"skill-{i}")
            results.append(r)
        return results

    results = asyncio.run(_run())

    # First 2 within limit, remaining 3 rate-limited
    assert len(lookup_calls) == 2, (
        f"Only 2 lookups should fire (limit=2), got {len(lookup_calls)}"
    )

    # Rate-limited queries return empty list
    for i in range(2, 5):
        assert results[i] == [], f"Rate-limited query {i} must return []"


def test_rate_limit_window_is_60_seconds():
    """Rate limit counter must reset after 60 seconds."""
    plugin, ctx = _make_plugin(max_lookups=2)

    lookup_calls = []

    async def _mock_find_providers(value):
        lookup_calls.append(value)
        return []

    plugin._lookup.find_providers = _mock_find_providers

    # Pre-populate the log with timestamps older than 60s
    plugin._query_lookup_log = [time.monotonic() - 61.0, time.monotonic() - 61.0]

    # Now queries should work again (old timestamps expired)
    async def _run():
        return await plugin.on_query("name", "fresh-skill")

    asyncio.run(_run())

    assert len(lookup_calls) == 1, "Lookup must fire after rate limit window resets"


def test_passive_mode_no_lookups():
    """Passive mode must never trigger lookups (no lookup module)."""
    plugin, ctx = _make_plugin(max_lookups=5, mode="passive")
    assert plugin._lookup is None

    lookup_log_before = list(plugin._query_lookup_log)
    asyncio.run(plugin.on_query("name", "passive-skill"))
    # No crash, no lookup recorded
    assert plugin._query_lookup_log == lookup_log_before
