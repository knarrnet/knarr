import pytest
from knarr.core.messages import Announce, sign_message, SIGNATURE_EXCLUDED_FIELDS
from nacl.signing import SigningKey

def test_sidecar_port_in_announce():
    msg = Announce(
        node_id="test",
        skill_key="skill",
        skill_sheet={},
        hops=0,
        sidecar_port=9001
    )
    d = msg.to_dict()
    assert d["sidecar_port"] == 9001

def test_sidecar_port_excluded_from_signature():
    sk = SigningKey.generate()
    msg_id = "same-id"
    msg1 = Announce(msg_id=msg_id, node_id="n", skill_key="k", sidecar_port=0)
    msg2 = Announce(msg_id=msg_id, node_id="n", skill_key="k", sidecar_port=9999)
    
    signed1 = sign_message(msg1, sk)
    signed2 = sign_message(msg2, sk)
    
    # Signatures should match because sidecar_port is excluded
    # print(f"SIGNATURE_EXCLUDED_FIELDS: {SIGNATURE_EXCLUDED_FIELDS}")
    assert "sidecar_port" in SIGNATURE_EXCLUDED_FIELDS
    assert signed1.signature == signed2.signature
    assert "sidecar_port" in SIGNATURE_EXCLUDED_FIELDS

def test_announce_defaults():
    msg = Announce()
    assert msg.sidecar_port == 0
