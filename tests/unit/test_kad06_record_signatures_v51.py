"""KAD-06: Provider record signatures tests."""
import sys
import os
import asyncio
import json
import hashlib
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'plugins', '00-kademlia'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from unittest.mock import MagicMock, AsyncMock


def _make_signing_key():
    from nacl.signing import SigningKey
    return SigningKey.generate()


def _sign_bytes_factory(signing_key):
    """Create a sign_bytes callable like plugin_bridge._SignBytesCallback."""
    def _sign(data: bytes):
        sig = signing_key.sign(data).signature
        pubkey_hex = signing_key.verify_key.encode().hex()
        return (sig, pubkey_hex)
    return _sign


def _make_ctx(sign_bytes=None, node_id=None):
    ctx = MagicMock()
    ctx.node_id = node_id or "a" * 64
    ctx.get_peers = MagicMock(return_value=[])
    ctx.send_fire_forget = AsyncMock()
    ctx.log = MagicMock()
    ctx.sign_bytes = sign_bytes
    return ctx


def _make_plugin(sign_bytes=None, k=4):
    ctx = _make_ctx(sign_bytes=sign_bytes)
    config = {"mode": "full", "k": k, "debug": False}
    from handler import KademliaPlugin
    plugin = KademliaPlugin(ctx, config)
    return plugin, ctx


def _make_signed_payload(sign_bytes_fn, skill_key="signed-skill", plugin=None, sender_id=None):
    """Build a properly signed PUT_PROVIDER payload with token."""
    payload = {
        "skill_key": skill_key,
        "canonical_path": skill_key,
        "node_id": "a" * 64,
        "host": "",
        "port": 0,
        "sidecar_port": 0,
        "ttl": 3600,
        "published_at": 0.0,
    }
    # KAD-11: add token
    if plugin is not None and sender_id is not None:
        payload["_token"] = plugin._generate_token(sender_id)

    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    sig_bytes, pubkey_hex = sign_bytes_fn(payload_bytes)
    payload["_sig"] = sig_bytes.hex()
    payload["_pubkey"] = pubkey_hex
    return payload


def _make_put_msg(sender_id, payload):
    from knarr.core.messages import PluginMessage
    return PluginMessage(
        node_id=sender_id,
        plugin_name="knarr-kademlia",
        action="PUT_PROVIDER",
        payload=json.dumps(payload),
    )


def _make_peers(node_id):
    from knarr.core.models import NodeInfo
    return [NodeInfo(node_id=node_id, host="10.0.0.1", port=9001)]


def test_signed_record_accepted():
    """Properly signed provider record must be accepted."""
    sk = _make_signing_key()
    sign_bytes = _sign_bytes_factory(sk)
    plugin, ctx = _make_plugin(sign_bytes=sign_bytes)

    skill_key = "signed-skill"
    # TP-2: sender_id must be SHA-256 of the verify key for identity binding
    sender_id = hashlib.sha256(sk.verify_key.encode()).hexdigest()
    payload = _make_signed_payload(sign_bytes, skill_key, plugin=plugin, sender_id=sender_id)

    msg = _make_put_msg(sender_id, payload)
    asyncio.run(plugin._handle_put_provider(msg, payload, _make_peers(sender_id)))

    results = plugin.providers.get_providers(skill_key)
    assert len(results) == 1, "Properly signed record must be accepted"


def test_record_with_invalid_signature_rejected():
    """Record with invalid (tampered) signature must be rejected."""
    sk = _make_signing_key()
    sign_bytes = _sign_bytes_factory(sk)
    plugin, ctx = _make_plugin(sign_bytes=sign_bytes)

    skill_key = "tampered-skill"
    # TP-2: sender_id must be SHA-256 of the verify key for identity binding
    sender_id = hashlib.sha256(sk.verify_key.encode()).hexdigest()
    payload = _make_signed_payload(sign_bytes, skill_key, plugin=plugin, sender_id=sender_id)

    # Tamper the signature
    sig_bytes = bytes.fromhex(payload["_sig"])
    tampered = bytes([sig_bytes[0] ^ 0xFF]) + sig_bytes[1:]
    payload["_sig"] = tampered.hex()

    msg = _make_put_msg(sender_id, payload)
    asyncio.run(plugin._handle_put_provider(msg, payload, _make_peers(sender_id)))

    results = plugin.providers.get_providers(skill_key)
    assert len(results) == 0, "Tampered signature must be rejected"


def test_record_with_missing_signature_accepted():
    """TP-5: Record without _sig/_pubkey is accepted (verification is payload-triggered,
    not receiver-config-triggered). Identity binding (TP-2) guards signed records."""
    sk = _make_signing_key()
    sign_bytes = _sign_bytes_factory(sk)
    plugin, ctx = _make_plugin(sign_bytes=sign_bytes)

    skill_key = "unsigned-skill"
    sender_id = "d" * 64
    # Build payload without _sig / _pubkey but WITH token
    payload = {
        "skill_key": skill_key,
        "canonical_path": skill_key,
        "node_id": "a" * 64,
        "host": "",
        "port": 0,
        "sidecar_port": 0,
        "ttl": 3600,
        "published_at": 0.0,
        "_token": plugin._generate_token(sender_id),
    }

    msg = _make_put_msg(sender_id, payload)
    asyncio.run(plugin._handle_put_provider(msg, payload, _make_peers(sender_id)))

    results = plugin.providers.get_providers(skill_key)
    assert len(results) == 1, "TP-5: Unsigned record accepted (verification is payload-triggered)"


def test_no_sign_bytes_no_verification():
    """When sign_bytes is None (not configured), signature is not required."""
    plugin, ctx = _make_plugin(sign_bytes=None)

    skill_key = "unsigned-ok-skill"
    sender_id = "e" * 64
    # Need valid token
    payload = {
        "skill_key": skill_key,
        "canonical_path": skill_key,
        "node_id": "a" * 64,
        "host": "",
        "port": 0,
        "sidecar_port": 0,
        "ttl": 3600,
        "published_at": 0.0,
        "_token": plugin._generate_token(sender_id),
    }

    msg = _make_put_msg(sender_id, payload)
    asyncio.run(plugin._handle_put_provider(msg, payload, _make_peers(sender_id)))

    results = plugin.providers.get_providers(skill_key)
    assert len(results) == 1, "Unsigned record must be accepted when sign_bytes not configured"


def test_sign_bytes_callback_from_plugin_bridge():
    """_SignBytesCallback from plugin_bridge must produce verifiable signatures."""
    from knarr.commerce.plugin_bridge import _SignBytesCallback
    from nacl.signing import SigningKey, VerifyKey

    sk = SigningKey.generate()
    cb = _SignBytesCallback(sk)

    data = b"test record bytes"
    sig_bytes, pubkey_hex = cb(data)

    # Verify
    vk = VerifyKey(bytes.fromhex(pubkey_hex))
    vk.verify(data, sig_bytes)  # raises if invalid
