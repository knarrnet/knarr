import hashlib
import json
import pytest
from nacl.signing import SigningKey
from knarr.core.messages import (
    Announce, sign_message, verify_message, verify_node_id, Message
)

def test_keypair_derivation_consistent():
    seed = b"test_seed_1234567890123456789012" # 32 bytes
    key1 = SigningKey(seed)
    key2 = SigningKey(seed)
    assert key1.verify_key.encode() == key2.verify_key.encode()

def test_node_id_is_sha256_of_public_key():
    seed = b"a" * 32
    key = SigningKey(seed)
    public_key = key.verify_key.encode()
    node_id = hashlib.sha256(public_key).hexdigest()
    
    # Check that this matches our logic in messages.py
    msg = Announce(node_id=node_id)
    signed = sign_message(msg, key)
    assert verify_node_id(signed) == True

def test_sign_and_verify_roundtrip():
    key = SigningKey.generate()
    msg = Announce(node_id="n1", skill_key="k", skill_sheet={"name":"k", "tags":["t"]})
    signed = sign_message(msg, key)
    assert verify_message(signed) == True

def test_invalid_signature_rejected():
    key = SigningKey.generate()
    msg = Announce(node_id="n1", skill_key="k", skill_sheet={"name":"k", "tags":["t"]})
    signed = sign_message(msg, key)
    
    # Tamper with the payload
    # Since Message is frozen, we reconstruct
    d = signed.to_dict()
    d["skill_key"] = "tampered"
    tampered = Announce(**d)
    
    assert verify_message(tampered) == False

def test_wrong_key_rejected():
    key_a = SigningKey.generate()
    key_b = SigningKey.generate()
    msg = Announce(node_id="n1", skill_key="k", skill_sheet={"name":"k", "tags":["t"]})
    signed_a = sign_message(msg, key_a)
    
    # Reconstruct with wrong public key but original signature
    d = signed_a.to_dict()
    d["public_key"] = key_b.verify_key.encode().hex()
    wrong_key_msg = Announce(**d)
    
    assert verify_message(wrong_key_msg) == False

def test_hops_excluded_from_signature():
    """hops remains in SIGNATURE_EXCLUDED_FIELDS for Announce relay compatibility.

    TP-7 fix: on-path hops suppression for EventNotify is addressed by removing
    the hops>0 gate in _handle_event_notify (loop prevention via origin_node marker),
    NOT by signing hops globally (which would break Announce relay).
    """
    key = SigningKey.generate()
    msg = Announce(node_id="n1", skill_key="k", skill_sheet={"name":"k", "tags":["t"]}, hops=0)
    signed = sign_message(msg, key)

    # Change hops — relay nodes can increment without breaking signature
    d = signed.to_dict()
    d["hops"] = 1
    msg_v1 = Announce(**d)

    assert verify_message(msg_v1) == True

def test_canonical_json_deterministic():
    d = {"b": 2, "a": 1, "c": {"z": 0, "y": 9}}
    c1 = json.dumps(d, sort_keys=True, separators=(',', ':'))
    c2 = json.dumps(d, sort_keys=True, separators=(',', ':'))
    assert c1 == c2
    assert c1 == '{"a":1,"b":2,"c":{"y":9,"z":0}}'
