"""Sentinel tests for V0.24.0 Core Hardening constraints."""
from knarr.core.messages import Announce, SIGNATURE_EXCLUDED_FIELDS
from knarr.core.wallet import b58encode
from nacl.signing import SigningKey

def test_announce_wallet_exclusion():
    """Ensure 'wallet' is excluded from signatures for backward compatibility."""
    assert "wallet" in SIGNATURE_EXCLUDED_FIELDS

def test_wallet_derivation_deterministic():
    """Ensure derivation from Ed25519 public key to base58 Solana valid length is deterministic."""
    sk = SigningKey.generate()
    vk_bytes = sk.verify_key.encode()
    wallet_addr = b58encode(vk_bytes)

    # Needs to be 32 bytes encoded = 43 or 44 chars in base58
    assert 32 <= len(wallet_addr) <= 44

    # Deterministic check
    assert wallet_addr == b58encode(vk_bytes)


def test_wallet_is_not_node_id():
    """wallet (base58 of pubkey) != node_id (sha256 hex of pubkey)."""
    import hashlib
    sk = SigningKey.generate()
    vk_bytes = sk.verify_key.encode()
    wallet = b58encode(vk_bytes)
    node_id = hashlib.sha256(vk_bytes).hexdigest()
    assert wallet != node_id


def test_wallet_is_not_encryption_key():
    """wallet (base58 of Ed25519 pubkey) != encryption_key (hex of X25519 pubkey)."""
    sk = SigningKey.generate()
    wallet = b58encode(sk.verify_key.encode())
    encryption_key = sk.verify_key.to_curve25519_public_key().encode().hex()
    assert wallet != encryption_key


def test_egress_filter_no_config_flag():
    """Egress filter must have no disable/toggle/enabled attribute or parameter."""
    from knarr.core.egress_filter import EgressFilter
    # No attribute or method to disable the filter
    members = dir(EgressFilter)
    for name in members:
        name_lower = name.lower()
        assert "disable" not in name_lower, f"EgressFilter has disable-like member: {name}"
        assert "toggle" not in name_lower, f"EgressFilter has toggle-like member: {name}"
        assert "enabled" not in name_lower, f"EgressFilter has enabled-like member: {name}"
    # Constructor takes no arguments (no config injection)
    import inspect
    sig = inspect.signature(EgressFilter.__init__)
    # Only 'self' parameter
    assert list(sig.parameters.keys()) == ["self"], f"EgressFilter.__init__ takes unexpected params: {list(sig.parameters.keys())}"


def test_credit_balancer_loop_deleted():
    """_credit_balancer_loop must not exist in DHTNode."""
    from knarr.dht.node import DHTNode
    assert not hasattr(DHTNode, "_credit_balancer_loop")
