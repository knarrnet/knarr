"""A-02 tests: trace/debug plumbing plus S-01 regressions."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


def test_query_plugin_trace_id_forwarded_to_node():
    """PluginContext.query_plugin forwards trace_id to node.query_plugin.

    T1-01 moved the RPC implementation to DHTNode.query_plugin. PluginContext
    is a pure delegate. This test verifies trace_id is passed through correctly.
    """
    from knarr.dht.plugins import PluginContext

    node = MagicMock()
    node.query_plugin = AsyncMock(return_value={"ok": True, "trace_id": "trace-abc"})

    ctx = PluginContext.__new__(PluginContext)
    ctx._node = node
    ctx.node_id = "a" * 64

    async def _run():
        result = await ctx.query_plugin(
            "b" * 64,
            "127.0.0.1",
            9010,
            "knarr-punchhole",
            "REQUEST",
            {"object_key": "skills"},
            timeout=1.0,
            trace_id="trace-abc",
        )
        node.query_plugin.assert_awaited_once()
        call_kwargs = node.query_plugin.call_args[1]
        assert call_kwargs.get("trace_id") == "trace-abc", (
            "trace_id must be forwarded to node.query_plugin"
        )
        assert result["ok"] is True

    asyncio.run(_run())


def test_plugin_context_exposes_debug_flag_and_config_dir():
    """PluginContext stores plugin debug config and config_dir."""
    from knarr.dht.plugins import PluginContext

    ctx = PluginContext(
        node_id="a" * 64,
        plugin_dir=Path("/tmp/plugin"),
        config_dir=Path("/tmp/config"),
        config={"debug": True},
    )
    assert ctx._debug is True
    assert ctx.config_dir == Path("/tmp/config")


def test_c3_circuit_breaker_removed():
    """Dead C3 breaker identifiers are gone from SyncEngine."""
    import inspect
    from knarr.mail.sync import SyncEngine
    source = inspect.getsource(SyncEngine)
    assert "_circuit_state" not in source, \
        "C3 _circuit_state should be deleted (dead code, marked in v0.53.1)"
    assert "_circuit_allows" not in source, \
        "C3 _circuit_allows should be deleted"
    assert "_circuit_on_success" not in source, \
        "C3 _circuit_on_success should be deleted"
    assert "_circuit_on_failure" not in source, \
        "C3 _circuit_on_failure should be deleted"
