"""B3 contract test: PEER_SWEEP batch cap — O(n) event loop block.

P-025: _peer_heartbeat_sweep iterates over ALL peers in one synchronous pass.
At 100 peers with one network call per peer: 10s+ asyncio block. At 1000 peers: ~100s.
This starves task handlers and kills throughput at scale.

FIX LOCATION: node.py:_peer_heartbeat_sweep() and/or _peer_heartbeat_sweep_loop()
Cap the batch at N peers per cycle, rotating the starting position so all peers
are eventually visited:

    SWEEP_BATCH_SIZE = 50  # peers per cycle
    # In _peer_heartbeat_sweep or _peer_heartbeat_sweep_loop:
    offset = getattr(self, '_sweep_offset', 0)
    batch = peers[offset:offset + SWEEP_BATCH_SIZE]
    self._sweep_offset = (offset + SWEEP_BATCH_SIZE) % max(len(peers), 1)

CONTRACT:
- _peer_heartbeat_sweep must process at most SWEEP_BATCH_SIZE peers per cycle
  when peer list exceeds SWEEP_BATCH_SIZE.
- SWEEP_BATCH_SIZE must be <= 100 (reasonable cap; > 100 doesn't solve the O(n)
  problem at 1000 nodes).
- Over sufficient cycles, all peers must be visited at least once (rotation).
- With < SWEEP_BATCH_SIZE peers, all are processed each cycle.
"""
import asyncio
import time
import pytest
from unittest.mock import MagicMock, AsyncMock


def _make_peer(i):
    peer = MagicMock()
    peer.node_id = f"{i:04x}" + "00" * 30
    peer.host = "10.0.0.1"
    peer.port = 9010
    return peer


def _make_peers(count):
    return [_make_peer(i) for i in range(count)]


def _make_node(peers, stale=True):
    """Build a minimal DHTNode stub with peer activity pre-populated."""
    from knarr.dht.node import DHTNode

    node = DHTNode.__new__(DHTNode)
    node.node_info = MagicMock()
    node.node_info.node_id = "ff" * 32
    node._peer_dead_timeout = 300
    node._heartbeat_silence_threshold = 60
    node._debug = False
    node.bus = MagicMock()
    node.storage = MagicMock()
    node._pool = MagicMock()
    node._pool.remove = AsyncMock()
    node._enqueue_write = AsyncMock()
    node._enqueue_write_proto = AsyncMock()
    node._sync = MagicMock()
    node._sync.push_to_peer = AsyncMock()
    node.resolve_peer = MagicMock(return_value=("10.0.0.1", 9010))
    node._sign = MagicMock(return_value=b"\x00" * 64)  # bypass crypto in _peer_heartbeat_sweep
    node._sweep_offset = 0

    now = time.monotonic()
    # stale=True: silence=90s > threshold=60s < dead=300s → HB send triggered
    # stale=False: silence=0s → no HB send
    activity_time = now - 90 if stale else now
    node._peer_last_activity = {p.node_id: activity_time for p in peers}

    return node, now


@pytest.mark.asyncio
async def test_sweep_processes_at_most_batch_size_peers():
    """With 200 stale peers, one sweep cycle must attempt HB to at most SWEEP_BATCH_SIZE."""
    peers = _make_peers(200)
    node, now = _make_node(peers, stale=True)

    hb_targets = []

    async def mock_send(node_id, host, port, msg):
        hb_targets.append(node_id)
        raise ConnectionError("simulated unreachable")  # no HB response needed

    node._pool.send = AsyncMock(side_effect=mock_send)

    await node._peer_heartbeat_sweep(peers, now)

    assert len(hb_targets) <= 100, (
        f"Sweep attempted HB to {len(hb_targets)} peers in one cycle "
        f"(must be <= 100 = SWEEP_BATCH_SIZE). "
        "Fix: add batch slicing with _sweep_offset rotation in _peer_heartbeat_sweep."
    )


@pytest.mark.asyncio
async def test_sweep_visits_all_peers_across_cycles():
    """Over 10 sweep cycles, all 100 stale peers must receive a HB attempt."""
    peers = _make_peers(100)
    node, _ = _make_node(peers, stale=True)

    all_hb_targets = set()

    async def mock_send(node_id, host, port, msg):
        all_hb_targets.add(node_id)
        raise ConnectionError("simulated unreachable")

    node._pool.send = AsyncMock(side_effect=mock_send)

    # Run 10 cycles. With SWEEP_BATCH_SIZE=50: 2 cycles covers 100 peers.
    # 10 cycles is safe for any reasonable BATCH_SIZE >= 10.
    for _ in range(10):
        now = time.monotonic()
        await node._peer_heartbeat_sweep(peers, now)

    assert all_hb_targets == {p.node_id for p in peers}, (
        f"Only {len(all_hb_targets)}/{len(peers)} peers received HB attempt across 10 cycles. "
        "Fix: rotation must ensure all peers are visited over sufficient cycles."
    )


def test_sweep_batch_size_constant_is_reasonable():
    """SWEEP_BATCH_SIZE or equivalent must be defined and <= 100."""
    from knarr.dht import node as node_module

    batch_size = (
        getattr(node_module, "SWEEP_BATCH_SIZE", None)
        or getattr(node_module, "PEER_SWEEP_BATCH_SIZE", None)
        or getattr(node_module, "_SWEEP_BATCH", None)
    )

    if batch_size is None:
        pytest.skip(
            "SWEEP_BATCH_SIZE constant not yet defined — will be set by fix. "
            "Add SWEEP_BATCH_SIZE = 50 to node.py."
        )

    assert batch_size <= 100, (
        f"SWEEP_BATCH_SIZE={batch_size} is too large. "
        "Must be <= 100 to prevent event loop blocking at 1000 nodes."
    )
    assert batch_size >= 10, (
        f"SWEEP_BATCH_SIZE={batch_size} is too small. "
        "Must process at least 10 peers per cycle for reasonable convergence."
    )
