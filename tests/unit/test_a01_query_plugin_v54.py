"""A-01 tests: universal query_plugin primitive."""
import asyncio
import json
from unittest.mock import MagicMock, AsyncMock


def _make_node_stub():
    """Build a minimal DHTNode-like stub.

    T1-01 (v0.54.0) moved the RPC implementation from PluginContext.query_plugin
    to DHTNode.query_plugin. PluginContext.query_plugin is now a pure delegate.
    node.query_plugin must be an AsyncMock — plain MagicMock raises TypeError on await.
    """
    node = MagicMock()
    node._pending_rpcs = {}
    node.node_info = MagicMock()
    node.node_info.node_id = "a" * 64
    node.node_info.host = "127.0.0.1"
    node.node_info.port = 9030
    node._send_fire_forget = AsyncMock()
    node._signing_key = None
    node.query_plugin = AsyncMock(return_value=None)
    return node


def test_query_plugin_returns_none_on_timeout():
    """query_plugin returns None when the underlying RPC returns None (timeout/failure)."""
    from knarr.dht.plugins import PluginContext
    node = _make_node_stub()
    node.query_plugin = AsyncMock(return_value=None)
    ctx = PluginContext.__new__(PluginContext)
    ctx._node = node
    ctx.node_id = node.node_info.node_id

    async def _run():
        result = await ctx.query_plugin(
            "b" * 64, "127.0.0.1", 9010,
            "knarr-punchhole", "REQUEST",
            {"object_key": "skills"},
            timeout=0.1
        )
        assert result is None, "Should return None when node.query_plugin returns None"

    asyncio.run(_run())


def test_query_plugin_resolves_on_response():
    """query_plugin returns the response dict from node.query_plugin."""
    from knarr.dht.plugins import PluginContext
    node = _make_node_stub()
    node.query_plugin = AsyncMock(return_value={"data": {"test": True}})
    ctx = PluginContext.__new__(PluginContext)
    ctx._node = node
    ctx.node_id = node.node_info.node_id

    async def _run():
        result = await ctx.query_plugin(
            "b" * 64, "127.0.0.1", 9010,
            "knarr-punchhole", "REQUEST",
            {"object_key": "skills"},
            timeout=2.0
        )
        assert result is not None, "Should have resolved"
        assert result.get("data", {}).get("test") is True

    asyncio.run(_run())


def test_query_plugin_forwards_all_args():
    """PluginContext.query_plugin forwards all arguments to node.query_plugin."""
    from knarr.dht.plugins import PluginContext
    node = _make_node_stub()
    node.query_plugin = AsyncMock(return_value=None)
    ctx = PluginContext.__new__(PluginContext)
    ctx._node = node
    ctx.node_id = node.node_info.node_id

    async def _run():
        await ctx.query_plugin(
            "b" * 64, "127.0.0.1", 9010,
            "knarr-punchhole", "REQUEST",
            {"object_key": "skills"},
            timeout=3.0,
            trace_id="test-trace-abc",
        )
        node.query_plugin.assert_awaited_once()
        call_args = node.query_plugin.call_args
        assert call_args[0][0] == "b" * 64, "node_id must be forwarded"
        assert call_args[0][3] == "knarr-punchhole", "plugin_name must be forwarded"
        assert call_args[1].get("trace_id") == "test-trace-abc", "trace_id must be forwarded"

    asyncio.run(_run())


def test_query_plugin_returns_none_when_no_node():
    """PluginContext.query_plugin returns None when _node is not set."""
    from knarr.dht.plugins import PluginContext
    ctx = PluginContext.__new__(PluginContext)
    # No _node set

    async def _run():
        result = await ctx.query_plugin(
            "b" * 64, "127.0.0.1", 9010,
            "knarr-punchhole", "REQUEST",
            {"object_key": "skills"},
        )
        assert result is None, "Should return None with no backing node"

    asyncio.run(_run())


def test_node_resolves_pending_plugin_rpc_before_plugin_chain():
    """Matching PluginMessage responses resolve pending futures directly.

    _resolve_pending_plugin_rpc checks the authenticated sender (sha256 of public_key)
    against the stored expected_target. We patch _authenticated_node_id_from_message to
    return the expected target — the anti-hijacking defense is a separate concern tested
    by the adversary panel.
    """
    from unittest.mock import patch
    from knarr.core.messages import PluginMessage
    from knarr.dht.node import DHTNode

    node = DHTNode.__new__(DHTNode)
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        future = loop.create_future()
        target_id = "b" * 64
        node._pending_rpcs = {"rpc-123": (future, target_id)}
        msg = PluginMessage(
            node_id=target_id,
            plugin_name="knarr-punchhole",
            action="RESPONSE",
            payload=json.dumps({"_request_id": "rpc-123", "ok": True}),
        )
        with patch.object(DHTNode, "_authenticated_node_id_from_message", return_value=target_id):
            assert node._resolve_pending_plugin_rpc(msg) is True
        assert future.done() is True
        assert future.result()["ok"] is True
    finally:
        asyncio.set_event_loop(None)
        loop.close()
