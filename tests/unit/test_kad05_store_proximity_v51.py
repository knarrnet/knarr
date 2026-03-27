"""KAD-05: K-closest validation on STORE."""
import sys
import os
import hashlib
import asyncio
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'plugins', '01-kademlia'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from unittest.mock import MagicMock, AsyncMock
from kbuckets import KBucketTable


LOCAL_ID = "0" * 64


def _make_ctx():
    ctx = MagicMock()
    ctx.node_id = LOCAL_ID
    ctx.get_peers = MagicMock(return_value=[])
    ctx.send_fire_forget = AsyncMock()
    ctx.log = MagicMock()
    ctx.sign_bytes = None  # KAD-06: no signature verification in proximity tests
    return ctx


def _make_plugin(k=4):
    ctx = _make_ctx()
    config = {"mode": "full", "k": k, "debug": False}
    from handler import KademliaPlugin
    return KademliaPlugin(ctx, config), ctx


def _make_put_payload(plugin, sender_id, skill_key="test-skill"):
    """Build a PUT_PROVIDER payload with a valid token."""
    token = plugin._generate_token(sender_id)
    return {"skill_key": skill_key, "canonical_path": skill_key, "_token": token}


def _make_put_msg(plugin, sender_id, skill_key="test-skill"):
    from knarr.core.messages import PluginMessage
    payload = _make_put_payload(plugin, sender_id, skill_key)
    return PluginMessage(
        node_id=sender_id,
        plugin_name="knarr-kademlia",
        action="PUT_PROVIDER",
        payload=json.dumps(payload),
    ), payload


def _make_peers_for(node_id):
    from knarr.core.models import NodeInfo
    return [NodeInfo(node_id=node_id, host="10.0.0.1", port=9001)]


def test_any_sender_accepted_during_bootstrap():
    """Any sender accepted when fewer than K peers in routing table (bootstrap)."""
    plugin, ctx = _make_plugin(k=4)
    # No peers in routing table — bootstrap phase

    far_id = "f" * 64  # Maximally far from LOCAL_ID
    skill_key = "bootstrap-skill"
    msg, payload = _make_put_msg(plugin, far_id, skill_key)

    asyncio.run(plugin._handle_put_provider(msg, payload, _make_peers_for(far_id)))

    # Should have stored (bootstrap allows all)
    results = plugin.providers.get_providers(skill_key)
    assert len(results) == 1, "Bootstrap phase must accept all senders"


def test_near_sender_accepted_when_k_peers_known():
    """Near sender accepted when routing table has >= K entries."""
    plugin, ctx = _make_plugin(k=4)

    skill_key = "proximity-skill"
    key_hash = hashlib.sha256(skill_key.encode()).hexdigest()
    key_int = int(key_hash, 16)

    # Populate routing table with K different peers — use XOR-small distances
    # so they are "close" to the key hash
    for i in range(4):
        # XOR distance 1, 2, 4, 8 from key_hash → very close
        near_int = key_int ^ (1 << i)
        near_id = format(near_int % (2**256), '064x')
        plugin.kbuckets.add_peer(near_id, f"10.0.0.{i+1}", 9001 + i)

    # A sender with XOR distance 1 from key_hash — very close
    near_sender_int = key_int ^ 1
    near_sender = format(near_sender_int % (2**256), '064x')

    msg, payload = _make_put_msg(plugin, near_sender, skill_key)
    asyncio.run(plugin._handle_put_provider(
        msg, payload, _make_peers_for(near_sender)
    ))

    results = plugin.providers.get_providers(skill_key)
    assert len(results) == 1, "Near sender must be accepted"


def test_far_sender_rejected_when_k_peers_known():
    """Far sender rejected when routing table has >= K entries."""
    plugin, ctx = _make_plugin(k=4)

    skill_key = "proximity-skill-reject"
    key_hash = hashlib.sha256(skill_key.encode()).hexdigest()
    key_int = int(key_hash, 16)

    # Populate routing table with K different peers very close to the key
    for i in range(4):
        near_int = key_int ^ (1 << i)
        near_id = format(near_int % (2**256), '064x')
        plugin.kbuckets.add_peer(near_id, f"10.0.0.{i+1}", 9001 + i)

    # A sender with maximum XOR distance from key_hash
    far_int = key_int ^ ((1 << 255))
    far_sender = format(far_int % (2**256), '064x')

    msg, payload = _make_put_msg(plugin, far_sender, skill_key)
    asyncio.run(plugin._handle_put_provider(
        msg, payload, _make_peers_for(far_sender)
    ))

    results = plugin.providers.get_providers(skill_key)
    assert len(results) == 0, "Far sender must be rejected when K peers are known"
