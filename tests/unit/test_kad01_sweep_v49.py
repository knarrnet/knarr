import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_PLUGIN_DIR = Path(__file__).parent.parent.parent / "plugins/01-kademlia"


def _load_module(name, path):
    """Load a module from a specific file path, bypassing sys.modules cache."""
    plugin_dir = str(_PLUGIN_DIR)
    added = plugin_dir not in sys.path
    if added:
        sys.path.insert(0, plugin_dir)
    try:
        spec = importlib.util.spec_from_file_location(f"_kad_{name}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if added and plugin_dir in sys.path:
            sys.path.remove(plugin_dir)


kad_handler = _load_module("handler", _PLUGIN_DIR / "handler.py")


def _make_plugin():
    plugin = kad_handler.KademliaPlugin.__new__(kad_handler.KademliaPlugin)
    plugin._ctx = SimpleNamespace(
        push_to_peer=AsyncMock(),
        remove_peer=AsyncMock(),
    )
    plugin._log = MagicMock()
    plugin._sweep_bucket_idx = 0
    plugin.kbuckets = SimpleNamespace(
        buckets=[[] for _ in range(256)],
        get_bucket_stats=lambda: {},
        remove_peer=MagicMock(),
    )
    return plugin


@pytest.mark.asyncio
async def test_sweep_k_buckets_pings_eight_lrs_peers_when_not_well_covered(monkeypatch):
    plugin = _make_plugin()
    for idx in range(8):
        plugin.kbuckets.buckets[idx] = [[f"{idx + 1:064x}", "127.0.0.1", 9000 + idx, 100.0]]
    plugin.kbuckets.get_bucket_stats = lambda: {0: 1, 1: 1}
    monkeypatch.setattr(kad_handler.time, "monotonic", lambda: 150.0)

    await kad_handler.KademliaPlugin._sweep_k_buckets(plugin, SimpleNamespace(peer_count=1))
    # push_to_peer results are wrapped in create_task — yield to let them complete
    await asyncio.sleep(0)

    assert plugin._ctx.push_to_peer.call_count == 8
    assert plugin.kbuckets.remove_peer.call_count == 0
    assert plugin._sweep_bucket_idx == 8


@pytest.mark.asyncio
async def test_sweep_k_buckets_removes_dead_lrs_peer(monkeypatch):
    plugin = _make_plugin()
    plugin.kbuckets.buckets[0] = [["01" * 32, "127.0.0.1", 9000, 100.0]]
    monkeypatch.setattr(kad_handler.time, "monotonic", lambda: 230.5)

    await kad_handler.KademliaPlugin._sweep_k_buckets(plugin, SimpleNamespace(peer_count=1))
    # remove_peer result is wrapped in create_task — yield to let it complete
    await asyncio.sleep(0)

    plugin.kbuckets.remove_peer.assert_called_once_with("01" * 32)
    plugin._ctx.remove_peer.assert_called_once_with("01" * 32)
    plugin._ctx.push_to_peer.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_k_buckets_skips_ping_when_node_is_well_covered(monkeypatch):
    plugin = _make_plugin()
    plugin.kbuckets.buckets[0] = [["02" * 32, "127.0.0.1", 9001, 100.0]]
    plugin.kbuckets.get_bucket_stats = lambda: {0: 2, 1: 2, 2: 2, 3: 2, 4: 2}
    monkeypatch.setattr(kad_handler.time, "monotonic", lambda: 150.0)

    await kad_handler.KademliaPlugin._sweep_k_buckets(plugin, SimpleNamespace(peer_count=10))

    plugin._ctx.push_to_peer.assert_not_awaited()
    plugin.kbuckets.remove_peer.assert_not_called()
