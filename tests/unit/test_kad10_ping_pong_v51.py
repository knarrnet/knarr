"""KAD-10: PING / PONG RPC tests."""
import sys
import os
import asyncio
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'plugins', '00-kademlia'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from unittest.mock import MagicMock, AsyncMock, patch


def _make_ctx(node_id=None):
    ctx = MagicMock()
    ctx.node_id = node_id or "a" * 64
    ctx.get_peers = MagicMock(return_value=[])
    ctx.send_fire_forget = AsyncMock()
    ctx.log = MagicMock()
    return ctx


def _make_plugin(node_id=None, mode="passive"):
    ctx = _make_ctx(node_id)
    config = {"mode": mode, "k": 4, "debug": False}
    from handler import KademliaPlugin
    plugin = KademliaPlugin(ctx, config)
    return plugin, ctx


def _make_plugin_msg(action, payload, sender_id=None):
    from knarr.core.messages import PluginMessage
    msg = PluginMessage(
        node_id=sender_id or "b" * 64,
        plugin_name="knarr-kademlia",
        action=action,
        payload=json.dumps(payload),
    )
    return msg


def _make_peers(node_id=None):
    from knarr.core.models import NodeInfo
    nid = node_id or "b" * 64
    return [NodeInfo(node_id=nid, host="10.0.0.1", port=9001)]


def test_ping_generates_pong_response():
    """PING message should generate a PONG response sent back to the sender."""
    plugin, ctx = _make_plugin()
    sender_id = "b" * 64

    ping_msg = _make_plugin_msg("PING", {"_request_id": "req-123"}, sender_id)
    peers = _make_peers(sender_id)

    asyncio.run(plugin._handle_plugin_message(ping_msg, "10.0.0.1", peers))

    ctx.send_fire_forget.assert_called_once()
    call_args = ctx.send_fire_forget.call_args
    # Second arg is the PluginMessage
    sent_msg = call_args[0][1]
    assert sent_msg.action == "PONG"
    resp_payload = json.loads(sent_msg.payload)
    assert resp_payload.get("_request_id") == "req-123"


def test_ping_without_request_id_still_sends_pong():
    """PING without _request_id still sends a PONG (just with empty payload)."""
    plugin, ctx = _make_plugin()
    sender_id = "b" * 64

    ping_msg = _make_plugin_msg("PING", {}, sender_id)
    peers = _make_peers(sender_id)

    asyncio.run(plugin._handle_plugin_message(ping_msg, "10.0.0.1", peers))

    ctx.send_fire_forget.assert_called_once()
    sent_msg = ctx.send_fire_forget.call_args[0][1]
    assert sent_msg.action == "PONG"


def test_pong_tracked_as_liveness_confirmation():
    """PONG reception records the sender in _pong_received."""
    plugin, ctx = _make_plugin()
    sender_id = "c" * 64

    pong_msg = _make_plugin_msg("PONG", {"_request_id": "req-456"}, sender_id)
    peers = _make_peers(sender_id)

    asyncio.run(plugin._handle_plugin_message(pong_msg, "10.0.0.1", peers))

    assert sender_id in plugin._pong_received, (
        "Sender of PONG must be recorded in _pong_received"
    )


def test_pong_timestamp_recorded():
    """PONG records a monotonic timestamp for the sender."""
    import time
    plugin, ctx = _make_plugin()
    sender_id = "d" * 64

    before = time.monotonic()
    pong_msg = _make_plugin_msg("PONG", {}, sender_id)
    peers = _make_peers(sender_id)
    asyncio.run(plugin._handle_plugin_message(pong_msg, "10.0.0.1", peers))
    after = time.monotonic()

    ts = plugin._pong_received.get(sender_id, 0)
    assert before <= ts <= after


def test_no_pong_sent_on_pong_reception():
    """Receiving a PONG must NOT trigger another PONG — no loop."""
    plugin, ctx = _make_plugin()
    sender_id = "e" * 64

    pong_msg = _make_plugin_msg("PONG", {}, sender_id)
    peers = _make_peers(sender_id)

    asyncio.run(plugin._handle_plugin_message(pong_msg, "10.0.0.1", peers))

    # send_fire_forget should NOT have been called (no reply to a PONG)
    ctx.send_fire_forget.assert_not_called()
