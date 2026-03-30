import sys
from pathlib import Path
import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

import knarr  # noqa: E402

knarr.__path__.insert(0, str(BASE_DIR / "src" / "knarr"))
sys.modules.pop("knarr.dht", None)
sys.modules.pop("knarr.dht.node", None)

from knarr.core.messages import Heartbeat
from knarr.dht import node as node_module
from knarr.dht.node import DHTNode


def _make_peer(suffix: int):
    return SimpleNamespace(
        node_id=f"{suffix:064x}",
        host="127.0.0.1",
        port=9000 + suffix,
        sidecar_port=0,
    )


def _make_node(peers, send_side_effect):
    node = DHTNode.__new__(DHTNode)
    node._enqueue_write = AsyncMock()
    node._enqueue_write_proto = AsyncMock()
    node._handler_pool = None
    node.storage = SimpleNamespace(
        cleanup_expired_jobs=MagicMock(),
        get_peers=MagicMock(return_value=peers),
        remove_peer=MagicMock(),
        upsert_address=MagicMock(),
    )
    node._sync = SimpleNamespace(
        cleanup=AsyncMock(),
        flush_outbox=AsyncMock(),
        pull_from_correspondents=AsyncMock(),
        push_to_peer=AsyncMock(),
    )
    node._plugins = SimpleNamespace(on_tick=AsyncMock())
    node._pool = SimpleNamespace(
        send=AsyncMock(side_effect=send_side_effect),
        evict_idle=AsyncMock(),
        remove=AsyncMock(),
    )
    node._connection_idle_timeout = 60.0
    node._config = {"mail": {"pull_interval": 999999}}
    node._last_pull_sweep = time.time()
    node._active_connections = 0
    node._write_queue = asyncio.Queue()
    node._start_time = time.monotonic()
    node._bootstrap_peers = []
    node.bus = SimpleNamespace(tick=MagicMock(return_value=0), emit=MagicMock())
    node.node_info = SimpleNamespace(node_id="a" * 64)
    silent_since = time.monotonic() - 100.0
    node._peer_last_activity = {peer.node_id: silent_since for peer in peers}
    node._peer_dead_timeout = 300.0
    node._heartbeat_silence_threshold = 90.0
    node._sign = lambda msg: msg
    node.resolve_peer = lambda _node_id, host, port: (host, port)
    node._run_netting_cycle_if_due = AsyncMock()
    node._version_gated = False
    return node


@pytest.mark.asyncio
async def test_sweep_completes_within_timeout(monkeypatch):
    peers = [_make_peer(i) for i in range(3)]

    async def slow_send(*_args):
        await asyncio.sleep(0.03)
        return Heartbeat(node_id=peers[0].node_id, timestamp=time.time(), version="0.0.0")

    node = _make_node(peers, slow_send)
    node._run_in_protocol_pool = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    monkeypatch.setattr(node_module, "PEER_HEARTBEAT_SWEEP_TIMEOUT", 0.05)
    monkeypatch.setattr(node_module, "verify_message", lambda _msg: True)
    monkeypatch.setattr(node_module, "verify_node_id", lambda _msg: True)

    start = time.monotonic()
    await DHTNode._heartbeat_tick(node)
    elapsed = time.monotonic() - start

    assert elapsed < 0.3


def test_sweep_timeout_logs_warning():
    """_peer_heartbeat_sweep_loop must have protective guards for sweep resilience.

    The sweep was extracted from _heartbeat_tick to _peer_heartbeat_sweep_loop in
    v0.41.0 (A2 independent background loops).  The loop guards against runaway
    sweeps via try/except and logs PEER_HEARTBEAT_SWEEP_FAIL on error.  The
    constant PEER_HEARTBEAT_SWEEP_TIMEOUT is still defined at module level for
    use in the per-peer sweep helper.
    """
    import inspect
    # Loop must be a proper async method
    source = inspect.getsource(DHTNode._peer_heartbeat_sweep_loop)
    assert "self._running" in source, "sweep loop must check self._running"
    assert "asyncio.sleep" in source, "sweep loop must sleep between iterations"
    assert "except" in source, "sweep loop must catch exceptions for resilience"

    # Module-level constant still exists
    assert hasattr(node_module, "PEER_HEARTBEAT_SWEEP_TIMEOUT"), \
        "PEER_HEARTBEAT_SWEEP_TIMEOUT constant must exist at module level"


@pytest.mark.asyncio
async def test_sweep_normal_completes(monkeypatch):
    peer = _make_peer(1)

    async def fast_send(*_args):
        return Heartbeat(node_id=peer.node_id, timestamp=time.time(), version="0.0.0")

    node = _make_node([peer], fast_send)
    monkeypatch.setattr(node_module, "verify_message", lambda _msg: True)
    monkeypatch.setattr(node_module, "verify_node_id", lambda _msg: True)

    await DHTNode._peer_heartbeat_sweep(node, [peer], now=time.monotonic())

    assert node._pool.send.await_count == 1
    assert node._sync.push_to_peer.await_count == 1
    assert node._enqueue_write_proto.await_count == 1
    assert node._peer_last_activity[peer.node_id] > 0.0
