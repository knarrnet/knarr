"""P-01 tests: punchhole PluginMessage transport with mail fallback."""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from knarr.core.messages import PluginMessage
from knarr.core.models import NodeInfo


def _load_module():
    import knarr
    pkg_root = Path(knarr.__file__).parent
    handler_path = pkg_root / "plugins" / "08-punchhole-frontend" / "handler.py"
    spec = importlib.util.spec_from_file_location("punchhole_frontend_v54", handler_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _make_ctx(tmp_path, requester_node_id: str, emitted: list):
    def _emit_event(event: str, **fields):
        emitted.append({"event": event, **fields})

    return SimpleNamespace(
        state_dir=tmp_path,
        plugin_dir=tmp_path,
        subscribe_events=None,
        emit_event=_emit_event,
        get_peers=lambda: [NodeInfo(node_id=requester_node_id, host="127.0.0.1", port=9010)],
        send_fire_forget=AsyncMock(),
        node_id="a" * 64,
    )


def test_punchhole_on_inbound_replies_with_same_request_id(tmp_path):
    module = _load_module()
    module.verify_document = lambda payload, verify_key: True

    requester = "b" * 64
    emitted = []
    ctx = _make_ctx(tmp_path, requester, emitted)
    plugin = module.PunchholeFrontendPlugin(ctx, {"disclosure_log": "disc.db", "debug": True})
    plugin._backend_ready = True
    plugin._acl[requester] = "all_signed"
    plugin._cache[("skills", "all_signed")] = {"data": {"skills": []}, "stale": False}

    async def _run():
        result = await plugin.on_inbound(
            PluginMessage(
                node_id=requester,
                plugin_name="knarr-punchhole",
                action="REQUEST",
                payload=json.dumps({
                    "object_key": "skills",
                    "payload": {"signed": True},
                    "_request_id": "rpc-123",
                    "trace_id": "trace-123",
                }),
            ),
            "127.0.0.1",
        )
        # P-01 ruling: on_inbound returns True (handled internally, no firewall.blocked)
        assert result is True

    asyncio.run(_run())

    assert ctx.send_fire_forget.await_count == 1
    response = ctx.send_fire_forget.await_args.args[1]
    payload = json.loads(response.payload)
    assert response.action == "RESPONSE"
    assert payload["_request_id"] == "rpc-123"
    assert payload["trace_id"] == "trace-123"
    assert payload["status"] == "ok"
    assert payload["from_cache"] is True


def test_punchhole_on_inbound_cache_miss_emits_backend_event_and_response(tmp_path):
    module = _load_module()
    module.verify_document = lambda payload, verify_key: True

    requester = "c" * 64
    emitted = []
    ctx = _make_ctx(tmp_path, requester, emitted)
    plugin = module.PunchholeFrontendPlugin(ctx, {"disclosure_log": "disc.db"})
    plugin._backend_ready = True
    plugin._acl[requester] = "all_signed"

    async def _run():
        result = await plugin.on_inbound(
            PluginMessage(
                node_id=requester,
                plugin_name="knarr-punchhole",
                action="REQUEST",
                payload=json.dumps({
                    "object_key": "skills",
                    "payload": {"signed": True},
                    "_request_id": "rpc-456",
                }),
            ),
            "127.0.0.1",
        )
        # P-01 ruling: on_inbound returns True (handled internally, no firewall.blocked)
        assert result is True

    asyncio.run(_run())

    assert any(event["event"] == "cache.miss.data.skills" for event in emitted)
    response = ctx.send_fire_forget.await_args.args[1]
    payload = json.loads(response.payload)
    assert payload["_request_id"] == "rpc-456"
    assert payload["status"] == "miss"


def test_punchhole_mail_fallback_still_emits_response_event(tmp_path):
    module = _load_module()
    module.verify_document = lambda payload, verify_key: True

    requester = "d" * 64
    emitted = []
    ctx = _make_ctx(tmp_path, requester, emitted)
    plugin = module.PunchholeFrontendPlugin(ctx, {"disclosure_log": "disc.db"})
    plugin._backend_ready = True
    plugin._acl[requester] = "all_signed"
    plugin._cache[("skills", "all_signed")] = {"data": {"skills": ["x"]}, "stale": False}

    async def _run():
        await plugin.on_mail_received(
            "punchhole.request",
            requester,
            "target",
            {"action": "request", "object_key": "skills", "payload": {"signed": True}, "trace_id": "trace-mail"},
            None,
        )

    asyncio.run(_run())

    responses = [event for event in emitted if event["event"] == "punchhole.response"]
    assert len(responses) == 1
    assert responses[0]["object_key"] == "skills"
    assert responses[0]["from_cache"] is True
    assert responses[0]["trace_id"] == "trace-mail"
