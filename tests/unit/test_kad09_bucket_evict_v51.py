"""KAD-09: Ping-before-evict on full buckets."""
import sys
import os
import asyncio
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'plugins', '00-kademlia'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from unittest.mock import MagicMock, AsyncMock
from kbuckets import KBucketTable


LOCAL_ID = "0" * 64
_LOCAL_INT = 0


def _id(n: int) -> str:
    """XOR distance 32+n from LOCAL_ID (all land in bucket 5)."""
    return format(_LOCAL_INT ^ (32 + n), '064x')


def _make_ctx():
    ctx = MagicMock()
    ctx.node_id = LOCAL_ID
    ctx.get_peers = MagicMock(return_value=[])
    ctx.send_fire_forget = AsyncMock()
    ctx.log = MagicMock()
    ctx.sign_bytes = None
    return ctx


def _make_plugin(k=4, ping_timeout=10.0):
    ctx = _make_ctx()
    config = {"mode": "full", "k": k, "debug": False, "ping_timeout_seconds": ping_timeout}
    from handler import KademliaPlugin
    return KademliaPlugin(ctx, config), ctx


def _make_health():
    from knarr.dht.plugins import NodeHealth
    return NodeHealth(0.0, 0, 100, 0, 10, 120.0)


def _make_peers(ids_hosts):
    from knarr.core.models import NodeInfo
    return [NodeInfo(node_id=nid, host=host, port=9000 + i) for i, (nid, host) in enumerate(ids_hosts)]


def test_unresponsive_oldest_evicted_new_peer_added():
    """When oldest does not PONG within timeout, it must be evicted and new peer added."""
    plugin, ctx = _make_plugin(k=4, ping_timeout=0.001)  # very short timeout

    # Fill bucket with k=4 peers (ids 0..3)
    for i in range(4):
        plugin.kbuckets.add_peer(_id(i), f"10.0.0.{i}", 9001 + i)

    # The bucket is full. Adding a 5th triggers pending eviction.
    candidate_id = _id(4)
    plugin.kbuckets.add_peer(candidate_id, "10.0.0.5", 9005)

    # Oldest pending for eviction = _id(0)
    oldest_id = _id(0)
    pending = plugin.kbuckets.get_pending_evictions()
    assert oldest_id in pending, "Oldest must be in pending evictions"

    # Record the ping as already sent (and timed out)
    plugin._ping_sent_at[oldest_id] = time.monotonic() - 999.0  # already timed out

    # Make peer list include the oldest so PING could be sent
    peers = _make_peers([(oldest_id, "10.0.0.1")])

    # Run tick to resolve evictions
    asyncio.run(plugin._resolve_pending_evictions(peers))

    # oldest_id should be evicted, candidate added
    pending_after = plugin.kbuckets.get_pending_evictions()
    assert oldest_id not in pending_after

    # Check bucket contents: candidate_id should be there, oldest should be gone
    all_peers = set()
    for bucket in plugin.kbuckets.buckets:
        for peer in bucket:
            all_peers.add(peer[0])

    assert oldest_id not in all_peers, "Unresponsive oldest must be evicted"
    assert candidate_id in all_peers, "New candidate must be added after eviction"


def test_responsive_oldest_retained_new_peer_dropped():
    """When oldest responds with PONG, it must be retained and new candidate dropped."""
    plugin, ctx = _make_plugin(k=4, ping_timeout=99999.0)

    # Fill bucket
    for i in range(4):
        plugin.kbuckets.add_peer(_id(i), f"10.0.0.{i}", 9001 + i)

    candidate_id = _id(4)
    plugin.kbuckets.add_peer(candidate_id, "10.0.0.5", 9005)

    oldest_id = _id(0)

    # Simulate: PING sent at t0, PONG received after
    t0 = time.monotonic() - 5.0
    plugin._ping_sent_at[oldest_id] = t0
    plugin._pong_received[oldest_id] = t0 + 1.0  # PONG after PING

    peers = _make_peers([(oldest_id, "10.0.0.1")])
    asyncio.run(plugin._resolve_pending_evictions(peers))

    # oldest must be retained; candidate must be dropped
    all_peers = set()
    for bucket in plugin.kbuckets.buckets:
        for peer in bucket:
            all_peers.add(peer[0])

    assert oldest_id in all_peers, "Responsive oldest must be retained"
    assert candidate_id not in all_peers, "New candidate must be dropped"
