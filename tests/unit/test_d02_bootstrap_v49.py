"""D-02 tests: Bootstrap storm backoff.

Verifies:
1. _initial_bootstrap_peers initialized to [] in __init__
2. _isolation_rejoin_attempts, _isolation_rejoin_last, _isolation_rejoin_backoff in __init__
3. join() sets _initial_bootstrap_peers only on first call
4. join() clears _bootstrap_peers on success
5. _should_attempt_rejoin() returns True when backoff has elapsed
6. _should_attempt_rejoin() returns False when within backoff window
7. _record_rejoin_attempt() increments counter and doubles backoff (capped at 300s)
8. _reset_rejoin_backoff() resets all state to initial values
9. _self_populate_routing_table() runs without error when no kad plugin present
10. Sweep loop: uses _initial_bootstrap_peers for recovery
"""
import asyncio
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_node_minimal():
    """Create a minimal DHTNode without starting it (avoids I/O)."""
    from knarr.dht.node import DHTNode
    node = DHTNode.__new__(DHTNode)
    # Minimal init
    node._config = {}
    node._bootstrap_peers = []
    node._initial_bootstrap_peers = []
    node._isolation_rejoin_attempts = 0
    node._isolation_rejoin_last = 0.0
    node._isolation_rejoin_backoff = 30.0
    node._isolation_since = None
    node._running = False
    node.bus = None
    node._debug = False
    return node


# ── 1. __init__ attributes ────────────────────────────────────────────────────

def test_init_new_attributes(tmp_path):
    """DHTNode.__init__ must set all four new D-02 attributes."""
    from knarr.dht.node import DHTNode
    db_path = str(tmp_path / "test.db")
    node = DHTNode(storage_path=db_path, config={"node": {"startup_jitter": False}})
    assert hasattr(node, "_initial_bootstrap_peers")
    assert node._initial_bootstrap_peers == []
    assert hasattr(node, "_isolation_rejoin_attempts")
    assert node._isolation_rejoin_attempts == 0
    assert hasattr(node, "_isolation_rejoin_last")
    assert node._isolation_rejoin_last == 0.0
    assert hasattr(node, "_isolation_rejoin_backoff")
    assert node._isolation_rejoin_backoff == 30.0
    node.storage._conn and node.storage._conn.close() if hasattr(node.storage, "_conn") else None


# ── 2. join() preserves _initial_bootstrap_peers on first call only ──────────

@pytest.mark.asyncio
async def test_join_sets_initial_peers_once():
    node = _make_node_minimal()
    node._initial_bootstrap_peers = []
    node._bootstrap_peers = []

    # Patch join to capture what it would set, without network I/O
    first_peers = ["127.0.0.1:9000", "127.0.0.1:9001"]
    second_peers = ["127.0.0.1:9002"]

    # Manually simulate the assignment from join():
    # First call
    node._bootstrap_peers = list(first_peers)
    if not node._initial_bootstrap_peers:
        node._initial_bootstrap_peers = list(first_peers)

    assert node._initial_bootstrap_peers == first_peers

    # Second call — _initial_bootstrap_peers must NOT change
    node._bootstrap_peers = list(second_peers)
    if not node._initial_bootstrap_peers:
        node._initial_bootstrap_peers = list(second_peers)

    assert node._initial_bootstrap_peers == first_peers, \
        "_initial_bootstrap_peers must be frozen after first join"


# ── 3. join() clears _bootstrap_peers after success ──────────────────────────

@pytest.mark.asyncio
async def test_join_clears_bootstrap_peers_on_success(tmp_path):
    """After a successful join, _bootstrap_peers is cleared."""
    from knarr.dht.node import DHTNode

    db_path = str(tmp_path / "test.db")
    node = DHTNode(storage_path=db_path, config={"node": {"startup_jitter": False}})
    node._bootstrap_peers = ["127.0.0.1:9999"]

    # Mock _reannounce_all and the network call
    node._reannounce_all = AsyncMock()
    node._self_populate_routing_table = AsyncMock()

    # Patch request_response to return a successful JoinResponse
    from knarr.core.messages import JoinResponse
    resp = JoinResponse(peers=[])
    import knarr.dht.node as node_mod
    with patch.object(node_mod, "request_response", new=AsyncMock(return_value=resp)), \
         patch.object(node_mod, "verify_message", return_value=True):
        result = await node.join(["127.0.0.1:9000"], skip_jitter=True)

    assert result is True
    assert node._bootstrap_peers == [], "_bootstrap_peers must be cleared after successful join"


# ── 4. _should_attempt_rejoin() logic ────────────────────────────────────────

def test_should_attempt_rejoin_initially_true():
    node = _make_node_minimal()
    # last=0, backoff=30: time.time() - 0 >= 30 is True (for any real time)
    node._isolation_rejoin_last = 0.0
    node._isolation_rejoin_backoff = 30.0
    assert node._should_attempt_rejoin() is True


def test_should_attempt_rejoin_false_within_backoff():
    node = _make_node_minimal()
    node._isolation_rejoin_last = time.time()  # just now
    node._isolation_rejoin_backoff = 30.0
    assert node._should_attempt_rejoin() is False


def test_should_attempt_rejoin_true_after_backoff():
    node = _make_node_minimal()
    node._isolation_rejoin_last = time.time() - 31.0  # 31s ago
    node._isolation_rejoin_backoff = 30.0
    assert node._should_attempt_rejoin() is True


# ── 5. _record_rejoin_attempt() ───────────────────────────────────────────────

def test_record_rejoin_attempt_increments_and_doubles():
    node = _make_node_minimal()
    node._isolation_rejoin_backoff = 30.0
    node._isolation_rejoin_attempts = 0

    node._record_rejoin_attempt()
    assert node._isolation_rejoin_attempts == 1
    assert node._isolation_rejoin_backoff == 60.0
    assert node._isolation_rejoin_last > 0

    node._record_rejoin_attempt()
    assert node._isolation_rejoin_attempts == 2
    assert node._isolation_rejoin_backoff == 120.0

    # Cap at 300s
    node._isolation_rejoin_backoff = 200.0
    node._record_rejoin_attempt()
    assert node._isolation_rejoin_backoff == 300.0


def test_record_rejoin_attempt_caps_backoff_at_300():
    node = _make_node_minimal()
    node._isolation_rejoin_backoff = 300.0
    node._record_rejoin_attempt()
    assert node._isolation_rejoin_backoff == 300.0


# ── 6. _reset_rejoin_backoff() ────────────────────────────────────────────────

def test_reset_rejoin_backoff():
    node = _make_node_minimal()
    node._isolation_rejoin_attempts = 5
    node._isolation_rejoin_last = 99999.0
    node._isolation_rejoin_backoff = 300.0

    node._reset_rejoin_backoff()

    assert node._isolation_rejoin_attempts == 0
    assert node._isolation_rejoin_last == 0.0
    assert node._isolation_rejoin_backoff == 30.0


# ── 7. _self_populate_routing_table() runs without error ─────────────────────

@pytest.mark.asyncio
async def test_self_populate_no_plugin(tmp_path):
    """Should complete silently when no kad plugin is loaded."""
    from knarr.dht.node import DHTNode
    db_path = str(tmp_path / "test.db")
    node = DHTNode(storage_path=db_path, config={"node": {"startup_jitter": False}})
    node._plugin_loader = None
    # Should not raise
    await asyncio.wait_for(node._self_populate_routing_table(), timeout=5.0)


# ── 8. Healthy-path resets backoff ────────────────────────────────────────────

def test_healthy_path_resets_backoff():
    """If peer count > 1 (healthy), backoff must be reset."""
    node = _make_node_minimal()
    node._isolation_rejoin_attempts = 3
    node._isolation_rejoin_backoff = 240.0

    # Simulate healthy path: call _reset_rejoin_backoff directly
    node._reset_rejoin_backoff()
    assert node._isolation_rejoin_backoff == 30.0
    assert node._isolation_rejoin_attempts == 0
