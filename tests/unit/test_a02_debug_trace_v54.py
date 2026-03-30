"""A-02 tests: trace/debug plumbing plus S-01 regressions."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


def test_query_plugin_trace_id_propagates_through_send_and_receive():
    """query_plugin stamps trace_id, returns it, and emits debug logs."""
    from knarr.dht.plugins import PluginContext

    node = MagicMock()
    node._pending_rpcs = {}
    node._send_fire_forget = AsyncMock()

    ctx = PluginContext.__new__(PluginContext)
    ctx._node = node
    ctx.node_id = "a" * 64
    ctx._debug = True
    ctx.log = MagicMock()

    async def _run():
        async def _respond():
            await asyncio.sleep(0.05)
            request_id, entry = next(iter(node._pending_rpcs.items()))
            future = entry[0] if isinstance(entry, tuple) else entry
            sent_msg = node._send_fire_forget.call_args[0][1]
            sent_payload = json.loads(sent_msg.payload)
            future.set_result({
                "_request_id": request_id,
                "trace_id": sent_payload["trace_id"],
                "ok": True,
            })

        task = asyncio.create_task(_respond())
        result = await ctx.query_plugin(
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
        assert result["trace_id"]
        log_lines = [call.args[0] for call in ctx.log.info.call_args_list]
        assert any("QUERY_PLUGIN_SEND" in line for line in log_lines)
        assert any("QUERY_PLUGIN_RECV" in line for line in log_lines)
        assert any(result["trace_id"] in line for line in log_lines)

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
