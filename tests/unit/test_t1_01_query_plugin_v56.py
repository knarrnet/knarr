import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from knarr.dht.node import DHTNode, _current_identity
from knarr.dht.plugins import PluginContext


def _make_node_stub():
    node = DHTNode.__new__(DHTNode)
    node._pending_rpcs = {}
    node._debug = False
    node.node_info = SimpleNamespace(
        node_id="a" * 64,
        host="127.0.0.1",
        port=9030,
    )
    node._send_fire_forget = AsyncMock()
    return node


@pytest.mark.asyncio
async def test_dhtnode_query_plugin_returns_none_on_timeout_and_cleans_pending():
    node = _make_node_stub()

    result = await node.query_plugin(
        "b" * 64,
        "127.0.0.1",
        9010,
        "knarr-punchhole",
        "REQUEST",
        {"object_key": "skills"},
        timeout=0.01,
    )

    assert result is None
    assert node._pending_rpcs == {}


@pytest.mark.asyncio
async def test_dhtnode_query_plugin_resolves_matching_response():
    node = _make_node_stub()

    async def _respond():
        await asyncio.sleep(0.01)
        request_id, entry = next(iter(node._pending_rpcs.items()))
        future, _target = entry
        future.set_result({"_request_id": request_id, "ok": True})

    task = asyncio.create_task(_respond())
    result = await node.query_plugin(
        "b" * 64,
        "127.0.0.1",
        9010,
        "knarr-punchhole",
        "REQUEST",
        {"object_key": "skills"},
        timeout=1.0,
    )
    await task

    assert result["ok"] is True
    assert "trace_id" in result


@pytest.mark.asyncio
async def test_plugin_context_query_plugin_delegates_to_node():
    ctx = PluginContext.__new__(PluginContext)
    ctx._node = MagicMock()
    ctx._node.query_plugin = AsyncMock(return_value={"ok": True})

    result = await ctx.query_plugin(
        "b" * 64,
        "127.0.0.1",
        9010,
        "knarr-punchhole",
        "REQUEST",
        {"object_key": "skills"},
        timeout=1.0,
    )

    assert result == {"ok": True}
    ctx._node.query_plugin.assert_awaited_once_with(
        "b" * 64,
        "127.0.0.1",
        9010,
        "knarr-punchhole",
        "REQUEST",
        {"object_key": "skills"},
        timeout=1.0,
        trace_id="",
    )


def test_plugin_context_current_identity_name_returns_scoped_name():
    ctx = PluginContext.__new__(PluginContext)
    token = _current_identity.set(SimpleNamespace(name="alice"))
    try:
        assert ctx.current_identity_name() == "alice"
    finally:
        _current_identity.reset(token)


def test_plugin_context_current_identity_name_returns_none_without_identity():
    ctx = PluginContext.__new__(PluginContext)
    token = _current_identity.set(None)
    try:
        assert ctx.current_identity_name() is None
    finally:
        _current_identity.reset(token)
