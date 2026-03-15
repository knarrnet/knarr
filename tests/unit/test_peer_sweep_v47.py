import asyncio
import inspect
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from knarr.core.messages import Heartbeat
from knarr.dht import node as node_module
from knarr.dht.node import DHTNode


def _make_peer(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        node_id=f"{index:064x}",
        host="127.0.0.1",
        port=9000 + index,
        sidecar_port=0,
    )


def _make_node(peers: list[SimpleNamespace]) -> tuple[DHTNode, float]:
    node = DHTNode.__new__(DHTNode)
    node.node_info = SimpleNamespace(node_id="f" * 64)
    node.storage = SimpleNamespace(remove_peer=MagicMock(), upsert_address=MagicMock())
    node._enqueue_write = AsyncMock()
    node._enqueue_write_proto = AsyncMock()
    node._pool = SimpleNamespace(send=AsyncMock(), remove=AsyncMock())
    node._sync = SimpleNamespace(push_to_peer=AsyncMock())
    node._peer_dead_timeout = 300.0
    node._heartbeat_silence_threshold = 60.0
    node._sweep_offset = 0
    node._version_gated = False
    node._sign = lambda msg: msg
    node.resolve_peer = lambda _node_id, host, port: (host, port)
    node.bus = MagicMock()
    now = time.monotonic()
    stale_at = now - 120.0
    node._peer_last_activity = {peer.node_id: stale_at for peer in peers}
    return node, now


@pytest.mark.asyncio
async def test_sweep_with_twenty_peers_sends_all_heartbeats(monkeypatch):
    peers = [_make_peer(i) for i in range(20)]
    node, now = _make_node(peers)
    sent = []

    async def send(node_id, _host, _port, _msg):
        sent.append(node_id)
        return Heartbeat(node_id=node_id, timestamp=time.time(), version="0.0.0")

    node._pool.send = AsyncMock(side_effect=send)
    monkeypatch.setattr(node_module, "verify_message", lambda _msg: True)
    monkeypatch.setattr(node_module, "verify_node_id", lambda _msg: True)

    await DHTNode._peer_heartbeat_sweep(node, peers, now)

    assert sent == [peer.node_id for peer in peers]


@pytest.mark.asyncio
async def test_peer_failure_does_not_abort_remaining_sends(monkeypatch):
    peers = [_make_peer(i) for i in range(20)]
    node, now = _make_node(peers)
    sent = []

    async def send(node_id, _host, _port, _msg):
        sent.append(node_id)
        return Heartbeat(node_id=node_id, timestamp=time.time(), version="0.0.0")

    async def push_to_peer(node_id, _host, _port):
        if node_id == peers[5].node_id:
            raise RuntimeError("push failed")

    node._pool.send = AsyncMock(side_effect=send)
    node._sync.push_to_peer = AsyncMock(side_effect=push_to_peer)
    monkeypatch.setattr(node_module, "verify_message", lambda _msg: True)
    monkeypatch.setattr(node_module, "verify_node_id", lambda _msg: True)

    await DHTNode._peer_heartbeat_sweep(node, peers, now)

    assert sent == [peer.node_id for peer in peers]


@pytest.mark.asyncio
async def test_semaphore_caps_send_concurrency_at_ten(monkeypatch):
    peers = [_make_peer(i) for i in range(20)]
    node, now = _make_node(peers)
    current = 0
    max_current = 0

    async def send(node_id, _host, _port, _msg):
        nonlocal current, max_current
        current += 1
        max_current = max(max_current, current)
        await asyncio.sleep(0.05)
        current -= 1
        return Heartbeat(node_id=node_id, timestamp=time.time(), version="0.0.0")

    node._pool.send = AsyncMock(side_effect=send)
    monkeypatch.setattr(node_module, "verify_message", lambda _msg: True)
    monkeypatch.setattr(node_module, "verify_node_id", lambda _msg: True)

    await DHTNode._peer_heartbeat_sweep(node, peers, now)

    assert max_current <= 10
    assert max_current >= 2


def test_sweep_uses_gather_with_return_exceptions():
    source = inspect.getsource(DHTNode._peer_heartbeat_sweep)
    assert "asyncio.gather" in source
    assert "return_exceptions=True" in source
    assert "asyncio.Semaphore(10)" in source
