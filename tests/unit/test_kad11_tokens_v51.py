"""KAD-11: Announce tokens — GET-before-STORE (BEP-5 token model)."""
import sys
import os
import asyncio
import json
import time
import hashlib
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
    ctx.sign_bytes = None
    return ctx


def _make_plugin(k=4):
    ctx = _make_ctx()
    config = {"mode": "full", "k": k, "debug": False}
    from handler import KademliaPlugin
    return KademliaPlugin(ctx, config), ctx


def _make_peers(node_id):
    from knarr.core.models import NodeInfo
    return [NodeInfo(node_id=node_id, host="10.0.0.1", port=9001)]


def _make_put_payload(plugin, sender_id, token, skill_key="token-test"):
    return {
        "skill_key": skill_key,
        "canonical_path": skill_key,
        "_token": token,
    }


def _make_put_msg(sender_id, payload):
    from knarr.core.messages import PluginMessage
    return PluginMessage(
        node_id=sender_id,
        plugin_name="knarr-kademlia",
        action="PUT_PROVIDER",
        payload=json.dumps(payload),
    )


def test_valid_current_window_token_accepted():
    """PUT with a valid current-window token must be accepted."""
    plugin, ctx = _make_plugin()
    sender_id = "b" * 64
    token = plugin._generate_token(sender_id)

    payload = _make_put_payload(plugin, sender_id, token)
    msg = _make_put_msg(sender_id, payload)
    asyncio.run(plugin._handle_put_provider(msg, payload, _make_peers(sender_id)))

    results = plugin.providers.get_providers("token-test")
    assert len(results) == 1, "Valid current-window token must be accepted"


def test_valid_previous_window_token_accepted():
    """PUT with a token from the previous 5-minute window must be accepted (clock skew)."""
    plugin, ctx = _make_plugin()
    sender_id = "c" * 64

    # Generate token for the previous window
    window = int(time.time() // 300) - 1
    h = hashlib.sha256(
        sender_id.encode() + str(window).encode() + plugin._token_secret
    ).hexdigest()
    prev_token = h[:16]

    payload = _make_put_payload(plugin, sender_id, prev_token, skill_key="prev-window-skill")
    msg = _make_put_msg(sender_id, payload)
    asyncio.run(plugin._handle_put_provider(msg, payload, _make_peers(sender_id)))

    results = plugin.providers.get_providers("prev-window-skill")
    assert len(results) == 1, "Previous-window token must be accepted (clock skew)"


def test_missing_token_rejected():
    """PUT without _token field must be rejected."""
    plugin, ctx = _make_plugin()
    sender_id = "d" * 64

    payload = {
        "skill_key": "no-token-skill",
        "canonical_path": "no-token-skill",
    }
    msg = _make_put_msg(sender_id, payload)
    asyncio.run(plugin._handle_put_provider(msg, payload, _make_peers(sender_id)))

    results = plugin.providers.get_providers("no-token-skill")
    assert len(results) == 0, "Missing token must be rejected"


def test_invalid_token_rejected():
    """PUT with an invalid token must be rejected."""
    plugin, ctx = _make_plugin()
    sender_id = "e" * 64

    payload = _make_put_payload(plugin, sender_id, "deadbeef01234567", skill_key="bad-token-skill")
    msg = _make_put_msg(sender_id, payload)
    asyncio.run(plugin._handle_put_provider(msg, payload, _make_peers(sender_id)))

    results = plugin.providers.get_providers("bad-token-skill")
    assert len(results) == 0, "Invalid token must be rejected"


def test_token_from_different_sender_rejected():
    """Token generated for sender A must not be valid for sender B."""
    plugin, ctx = _make_plugin()
    sender_a = "a" * 64
    sender_b = "b" * 64

    token_for_a = plugin._generate_token(sender_a)

    # Sender B tries to use sender A's token
    payload = _make_put_payload(plugin, sender_b, token_for_a, skill_key="stolen-token-skill")
    msg = _make_put_msg(sender_b, payload)
    asyncio.run(plugin._handle_put_provider(msg, payload, _make_peers(sender_b)))

    results = plugin.providers.get_providers("stolen-token-skill")
    assert len(results) == 0, "Token from different sender must be rejected"


def test_get_providers_response_includes_token():
    """GET_PROVIDERS response must include a _token field."""
    plugin, ctx = _make_plugin()
    sender_id = "f" * 64

    from knarr.core.messages import PluginMessage
    msg = PluginMessage(
        node_id=sender_id,
        plugin_name="knarr-kademlia",
        action="GET_PROVIDERS",
        payload=json.dumps({"skill_key": "some-skill", "_request_id": "req-1"}),
    )
    peers = _make_peers(sender_id)

    asyncio.run(plugin._handle_get_providers(msg, {"skill_key": "some-skill", "_request_id": "req-1"}, peers))

    ctx.send_fire_forget.assert_called_once()
    resp_msg = ctx.send_fire_forget.call_args[0][1]
    resp_payload = json.loads(resp_msg.payload)
    assert "_token" in resp_payload, "GET_PROVIDERS response must include _token"
    assert len(resp_payload["_token"]) == 16, "Token must be 16 hex chars"
