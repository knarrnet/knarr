"""KAD-01: Bootstrap self-lookup wrapper tests."""
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


def _make_plugin(mode="passive", node_id=None):
    ctx = _make_ctx(node_id)
    config = {"mode": mode, "k": 4, "debug": False}
    from handler import KademliaPlugin
    return KademliaPlugin(ctx, config), ctx


def test_iterative_find_node_present_on_plugin():
    """iterative_find_node must be present as a method on KademliaPlugin."""
    plugin, _ = _make_plugin(mode="full")
    assert hasattr(plugin, "iterative_find_node"), (
        "iterative_find_node must be present on KademliaPlugin"
    )
    assert callable(plugin.iterative_find_node)


def test_delegates_to_lookup_module_when_available():
    """iterative_find_node must delegate to self._lookup.find_nodes in full mode."""
    plugin, _ = _make_plugin(mode="full")
    target = "b" * 64

    # Replace _lookup with a mock
    plugin._lookup = MagicMock()
    plugin._lookup.find_nodes = AsyncMock(return_value=[{"node_id": "c" * 64}])

    result = asyncio.run(plugin.iterative_find_node(target))

    plugin._lookup.find_nodes.assert_called_once_with(target)
    assert len(result) == 1
    assert result[0]["node_id"] == "c" * 64


def test_returns_empty_when_lookup_not_initialized():
    """iterative_find_node must return [] gracefully when lookup is None (passive mode)."""
    plugin, _ = _make_plugin(mode="passive")
    assert plugin._lookup is None, "Passive mode should have _lookup=None"

    result = asyncio.run(plugin.iterative_find_node("a" * 64))
    assert result == []


def test_returns_empty_on_lookup_exception():
    """iterative_find_node must return [] if lookup raises, never propagate."""
    plugin, _ = _make_plugin(mode="full")
    plugin._lookup = MagicMock()
    plugin._lookup.find_nodes = AsyncMock(side_effect=RuntimeError("network error"))

    result = asyncio.run(plugin.iterative_find_node("b" * 64))
    assert result == []
