"""B1+B2 contract test: DHT bootstrap stampede jitter + re-join on isolation.

P-024 (B1): No startup jitter. 24/100 nodes failed DHT join at cold start — all
100 nodes hit bootstrap simultaneously. Fix: randomize join delay 0–30s.

P-023 (B2): Re-join triggers only on empty peer list (len==0), not peer_count==1.
Nodes that join and receive only the bootstrap peer as their sole peer (peer_count=1)
never re-bootstrap — they're isolated forever. Fix: treat peer_count<=1 after >5min
as an isolation signal and re-attempt bootstrap.

These are bundled: jitter without re-join still leaves isolated nodes. Re-join
without jitter re-triggers the stampede on all re-joining nodes simultaneously.

FIX LOCATIONS:
- node.py:join() — add random sleep before first bootstrap attempt (B1, ~5 LOC)
- node.py:_peer_heartbeat_sweep_loop() — change `if not peers:` to
  `if len(peers) <= 1:` with >5min isolation timer (B2, ~30 LOC)

CONTRACT B1:
- join() must call asyncio.sleep() with a value in range [0, 30] before
  sending the first JoinRequest, when startup_jitter is enabled (default True).
- The jitter sleep happens at most once per join() call (not on retry iterations).

CONTRACT B2:
- _peer_heartbeat_sweep_loop() must trigger re-bootstrap when peer_count == 1
  AND the node has been at peer_count <= 1 for more than 5 minutes (300s).
- It must NOT trigger re-bootstrap immediately on peer_count == 1 (needs timeout).
- It must still trigger re-bootstrap on peer_count == 0 (existing behaviour preserved).
"""
import asyncio
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, call


# --- B1: Startup jitter tests ---

@pytest.mark.asyncio
async def test_join_sleeps_before_first_request():
    """join() must sleep with a random delay in [0, 30] before first JoinRequest."""
    from knarr.dht.node import DHTNode

    node = DHTNode.__new__(DHTNode)
    node.node_info = MagicMock()
    node.node_info.node_id = "aa" * 32
    node.node_info.host = "127.0.0.1"
    node.node_info.port = 9010
    node._ephemeral = False
    node._bootstrap_peers = []
    node._config = {}
    node.storage = MagicMock()
    node.bus = MagicMock()

    sleep_calls = []

    async def capture_sleep(duration):
        sleep_calls.append(duration)

    mock_response = MagicMock()
    mock_response.peers = []

    with patch("asyncio.sleep", side_effect=capture_sleep), \
         patch("knarr.dht.node.request_response", new=AsyncMock(return_value=mock_response)), \
         patch("knarr.dht.node.verify_message", return_value=False), \
         patch.object(node, "_sign", side_effect=lambda msg: msg):
        try:
            await node.join(["127.0.0.1:9999"])
        except Exception:
            pass

    jitter_sleeps = [s for s in sleep_calls if 0 <= s <= 30]
    assert len(jitter_sleeps) >= 1, (
        f"join() did not sleep with a jitter in [0, 30]. sleep calls: {sleep_calls}. "
        "Fix: add 'await asyncio.sleep(random.uniform(0, 30))' at the start of join()."
    )


@pytest.mark.asyncio
async def test_join_jitter_is_random():
    """join() jitter must be random (not always the same value)."""
    from knarr.dht.node import DHTNode

    jitter_values = set()

    for _ in range(10):
        node = DHTNode.__new__(DHTNode)
        node.node_info = MagicMock()
        node.node_info.node_id = "aa" * 32
        node.node_info.host = "127.0.0.1"
        node.node_info.port = 9010
        node._ephemeral = False
        node._bootstrap_peers = []
        node._config = {}
        node.storage = MagicMock()
        node.bus = MagicMock()

        sleep_calls = []

        async def capture_sleep(duration):
            sleep_calls.append(duration)

        with patch("asyncio.sleep", side_effect=capture_sleep), \
             patch("knarr.dht.node.request_response", new=AsyncMock(side_effect=Exception("no conn"))), \
             patch.object(node, "_sign", side_effect=lambda msg: msg):
            try:
                await node.join(["127.0.0.1:9999"])
            except Exception:
                pass

        for s in sleep_calls:
            if 0 <= s <= 30:
                jitter_values.add(round(s, 2))

    assert len(jitter_values) > 1, (
        f"Jitter values are all the same: {jitter_values}. "
        "Jitter must use random.uniform or equivalent, not a fixed value."
    )


# --- B2: Re-join on isolation tests ---

@pytest.mark.asyncio
async def test_sweep_loop_triggers_rejoin_on_single_peer_after_timeout():
    """_peer_heartbeat_sweep_loop must trigger re-bootstrap when peer_count==1 for >5min."""
    from knarr.dht.node import DHTNode

    node = DHTNode.__new__(DHTNode)
    node.node_info = MagicMock()
    node.node_info.node_id = "aa" * 32
    node._bootstrap_peers = ["127.0.0.1:9999"]
    node._running = True
    node._config = {"node": {"sweep_interval": 0.01}}
    node.bus = MagicMock()

    # Simulate: one peer (bootstrap only), node has been isolated for 310s
    mock_peer = MagicMock()
    mock_peer.node_id = "bootstrap" + "00" * 28

    join_called = []

    async def fake_join(peers):
        join_called.append(peers)
        node._running = False  # stop after first trigger

    node.join = fake_join
    node._peer_last_activity = {}

    # Isolation started 310s ago
    node._isolation_since = time.monotonic() - 310  # attribute set by fix

    node.storage = MagicMock()
    node.storage.get_peers = MagicMock(return_value=[mock_peer])

    with patch("asyncio.sleep", new=AsyncMock()), \
         patch.object(node, "_peer_heartbeat_sweep", new=AsyncMock()):
        try:
            await asyncio.wait_for(node._peer_heartbeat_sweep_loop(), timeout=1.0)
        except (asyncio.TimeoutError, Exception):
            pass

    assert len(join_called) >= 1, (
        "Re-bootstrap not triggered for node isolated at peer_count=1 for >5min. "
        "Fix: check len(peers) <= 1 with isolation timer in _peer_heartbeat_sweep_loop."
    )


@pytest.mark.asyncio
async def test_sweep_loop_does_not_rejoin_immediately_on_single_peer():
    """Re-bootstrap must NOT trigger immediately on peer_count==1 — needs timeout."""
    from knarr.dht.node import DHTNode

    node = DHTNode.__new__(DHTNode)
    node.node_info = MagicMock()
    node.node_info.node_id = "aa" * 32
    node._bootstrap_peers = ["127.0.0.1:9999"]
    node._running = True
    node._config = {"node": {"sweep_interval": 0.01}}
    node.bus = MagicMock()

    mock_peer = MagicMock()
    mock_peer.node_id = "bootstrap" + "00" * 28

    join_called = []

    async def fake_join(peers):
        join_called.append(peers)

    node.join = fake_join
    node._peer_last_activity = {}

    # Isolation just started (10s ago — well within 5min timeout)
    node._isolation_since = time.monotonic() - 10

    node.storage = MagicMock()
    node.storage.get_peers = MagicMock(return_value=[mock_peer])

    loop_count = [0]

    async def fake_sweep(peers, now):
        loop_count[0] += 1
        if loop_count[0] >= 3:
            node._running = False

    with patch("asyncio.sleep", new=AsyncMock()), \
         patch.object(node, "_peer_heartbeat_sweep", new=AsyncMock(side_effect=fake_sweep)):
        try:
            await asyncio.wait_for(node._peer_heartbeat_sweep_loop(), timeout=1.0)
        except (asyncio.TimeoutError, Exception):
            pass

    assert len(join_called) == 0, (
        "Re-bootstrap triggered too early (within 5min isolation window). "
        "Fix must wait for isolation timeout before re-bootstrapping."
    )


@pytest.mark.asyncio
async def test_sweep_loop_still_rebootstraps_on_zero_peers():
    """Existing behaviour: peer_count==0 must still trigger re-bootstrap."""
    from knarr.dht.node import DHTNode

    node = DHTNode.__new__(DHTNode)
    node.node_info = MagicMock()
    node.node_info.node_id = "aa" * 32
    node._bootstrap_peers = ["127.0.0.1:9999"]
    node._running = True
    node._config = {"node": {"sweep_interval": 0.01}}
    node.bus = MagicMock()
    node._peer_last_activity = {}

    join_called = []

    async def fake_join(peers):
        join_called.append(peers)
        node._running = False

    node.join = fake_join
    node.storage = MagicMock()
    node.storage.get_peers = MagicMock(return_value=[])  # zero peers

    with patch("asyncio.sleep", new=AsyncMock()):
        try:
            await asyncio.wait_for(node._peer_heartbeat_sweep_loop(), timeout=1.0)
        except (asyncio.TimeoutError, Exception):
            pass

    assert len(join_called) >= 1, (
        "Re-bootstrap not triggered for peer_count==0. Existing behaviour must be preserved."
    )
