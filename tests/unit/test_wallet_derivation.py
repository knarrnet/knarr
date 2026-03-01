"""Tests for Ed25519 → Solana wallet address derivation."""
import pytest
from nacl.signing import SigningKey

from knarr.core.wallet import b58encode, derive_solana_address

_B58_CHARS = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def test_derive_produces_valid_base58():
    """Output chars must be within base58 alphabet, length 32-44."""
    sk = SigningKey.generate()
    addr = derive_solana_address(sk)
    assert all(c in _B58_CHARS for c in addr)
    assert 32 <= len(addr) <= 44


def test_derive_deterministic():
    """Same seed produces same address every time."""
    seed = bytes(range(32))
    addr1 = derive_solana_address(SigningKey(seed))
    addr2 = derive_solana_address(SigningKey(seed))
    assert addr1 == addr2


def test_b58encode_known_vector():
    """Known Ed25519 pubkey → known Solana address."""
    seed = bytes(range(32))
    sk = SigningKey(seed)
    pubkey_bytes = sk.verify_key.encode()
    result = b58encode(pubkey_bytes)
    assert result == "FAe4sisG95oZ42w7buUn5qEE4TAnfTTFPiguZUHmhiF"


def test_b58encode_leading_zeros():
    """Leading zero bytes become '1' characters."""
    data = b"\x00\x00\x01"
    result = b58encode(data)
    assert result.startswith("11")
    assert len(result) == 3  # two '1's + one char for 0x01


def test_b58encode_empty():
    """Empty input produces empty output."""
    assert b58encode(b"") == ""
