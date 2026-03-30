"""A-01 tests: universal query_plugin primitive."""
import asyncio
import json
from unittest.mock import MagicMock, AsyncMock


def _make_node_stub():
    """Build a minimal DHTNode-like stub with _pending_rpcs."""
    node = MagicMock()
    node._pending_rpcs = {}
    node.node_info = MagicMock()
    node.node_info.node_id = "a" * 64
    node.node_info.host = "127.0.0.1"
    node.node_info.port = 9030
    node._send_fire_forget = AsyncMock()
    node._signing_key = None
    return node


def test_query_plugin_returns_none_on_timeout():
    """query_plugin returns None when no response arrives."""
    from knarr.dht.plugins import PluginContext
    node = _make_node_stub()
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
        assert result is None, "Should return None on timeout"

    asyncio.run(_run())


def test_query_plugin_resolves_on_response():
    """query_plugin resolves when the matching future is completed."""
    from knarr.dht.plugins import PluginContext
    node = _make_node_stub()
    ctx = PluginContext.__new__(PluginContext)
    ctx._node = node
    ctx.node_id = node.node_info.node_id

    async def _run():
        # Start query in background
        async def _respond_after_delay():
            await asyncio.sleep(0.05)
            # Find the pending request and resolve it
            for rid, entry in list(node._pending_rpcs.items()):
                future = entry[0] if isinstance(entry, tuple) else entry
                future.set_result({"object_key": "skills", "data": {"test": True}, "_request_id": rid})
                break

        task = asyncio.create_task(_respond_after_delay())
        result = await ctx.query_plugin(
            "b" * 64, "127.0.0.1", 9010,
            "knarr-punchhole", "REQUEST",
            {"object_key": "skills"},
            timeout=2.0
        )
        assert result is not None, "Should have resolved"
        assert result.get("data", {}).get("test") is True
        await task

    asyncio.run(_run())


def test_query_plugin_sends_request_id():
    """query_plugin includes _request_id in the outbound PluginMessage payload."""
    from knarr.dht.plugins import PluginContext
    node = _make_node_stub()
    ctx = PluginContext.__new__(PluginContext)
    ctx._node = node
    ctx.node_id = node.node_info.node_id

    async def _run():
        # Will timeout, but we can inspect the sent message
        try:
            await asyncio.wait_for(
                ctx.query_plugin("b" * 64, "127.0.0.1", 9010,
                                 "knarr-punchhole", "REQUEST", {"object_key": "skills"},
                                 timeout=0.1),
                timeout=0.5
            )
        except (asyncio.TimeoutError, Exception):
            pass
        # Check that send_fire_forget was called with a message containing _request_id
        assert node._send_fire_forget.called, "Should have sent a message"
        call_args = node._send_fire_forget.call_args
        msg = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("msg")
        payload = json.loads(msg.payload)
        assert "_request_id" in payload, "Payload must contain _request_id"

    asyncio.run(_run())


def test_pending_rpcs_cleaned_after_timeout():
    """_pending_rpcs entries are removed after timeout."""
    from knarr.dht.plugins import PluginContext
    node = _make_node_stub()
    ctx = PluginContext.__new__(PluginContext)
    ctx._node = node
    ctx.node_id = node.node_info.node_id

    async def _run():
        await ctx.query_plugin(
            "b" * 64, "127.0.0.1", 9010,
            "knarr-punchhole", "REQUEST",
            {"object_key": "skills"},
            timeout=0.1
        )
        assert len(node._pending_rpcs) == 0, f"Leaked entries: {node._pending_rpcs.keys()}"

    asyncio.run(_run())


def test_node_resolves_pending_plugin_rpc_before_plugin_chain():
    """Matching PluginMessage responses resolve pending futures directly."""
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
        assert node._resolve_pending_plugin_rpc(msg) is True
        assert future.done() is True
        assert future.result()["ok"] is True
    finally:
        asyncio.set_event_loop(None)
        loop.close()
