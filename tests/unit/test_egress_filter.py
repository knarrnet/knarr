"""Tests for egress filter."""
import pytest
from knarr.core.egress_filter import EgressFilter

def test_egress_filter_text_clean():
    f = EgressFilter()
    assert f.check("Hello world this is a safe string") is True

def test_egress_filter_path_blocked():
    f = EgressFilter()
    assert f.check("Error opening node.key") is False
    assert f.check("Checking .knarr/keyring for files") is False
    
def test_egress_filter_raw_bytes_blocked():
    f = EgressFilter()
    secret = b"supers3cr3tk3y1234567890abcdef"
    f.register_sensitive_material(secret)
    # text output contains hex 
    assert f.check(f"leaked {secret.hex()}") is False

def test_egress_filter_b58_blocked():
    f = EgressFilter()
    secret = bytes(range(64)) # typical key size
    f.register_sensitive_material(secret)
    from knarr.core.wallet import b58encode
    b58 = b58encode(secret)
    assert f.check(f"leaked {b58}") is False

def test_egress_filter_binary_path_blocked():
    f = EgressFilter()
    binary_payload = b"\x00\x01\x02" + b"node.key" + b"\xff"
    assert f.check_binary(binary_payload) is False

def test_egress_filter_binary_clean():
    f = EgressFilter()
    binary_payload = b"\x00\x01\x02\xff\xfe"
    assert f.check_binary(binary_payload) is True


def test_egress_filter_case_insensitive_paths():
    f = EgressFilter()
    assert f.check("Error opening HOT.KEY") is False
    assert f.check("found RESERVE.KEY on disk") is False
    assert f.check("reading .KNARR/KEYRING/secret") is False


def test_egress_filter_register_multiple():
    f = EgressFilter()
    key1 = b"firstkey1234567890abcdef12345678"
    key2 = b"secondkey234567890abcdef1234567"
    key3 = b"thirdkey3234567890abcdef1234567"
    f.register_sensitive_material(key1)
    f.register_sensitive_material(key2)
    f.register_sensitive_material(key3)
    assert f.check(f"leaked {key1.hex()}") is False
    assert f.check(f"leaked {key2.hex()}") is False
    assert f.check(f"leaked {key3.hex()}") is False
    assert f.check("clean payload no keys here") is True


def test_egress_filter_empty_payload():
    f = EgressFilter()
    f.register_sensitive_material(b"secret_key_bytes_1234567890abcd")
    assert f.check("") is True


def test_egress_filter_uppercase_hex_blocked():
    """Uppercase hex encoding of key material must also be caught."""
    f = EgressFilter()
    secret = b"supers3cr3tk3y1234567890abcdef"
    f.register_sensitive_material(secret)
    assert f.check(f"leaked {secret.hex().upper()}") is False


def test_egress_filter_no_false_positive_b58():
    """Long base58 string that is NOT a registered key should pass."""
    f = EgressFilter()
    # Register one key
    real_key = b"realkey12345678901234567890abcdef"
    f.register_sensitive_material(real_key)
    # Craft a different long base58 string (NOT derived from real_key)
    fake_b58 = "1" * 88  # valid base58 chars but not our key
    assert f.check(f"address: {fake_b58}") is True
