import json
from knarr.core.messages import JoinRequest, sign_message, verify_message
from nacl.signing import SigningKey

def test_ephemeral_field_serialization():
    req = JoinRequest(node_id="n1", host="h1", port=9000, ephemeral=True)
    import json
    b = json.dumps(req.to_dict()).encode("utf-8")
    
    # Round trip
    from knarr.core.messages import deserialize_message
    req2 = deserialize_message(b)
    assert req2.ephemeral is True

def test_ephemeral_excluded_from_signature():
    sk = SigningKey.generate()
    req = JoinRequest(node_id="n1", host="h1", port=9000, ephemeral=False)
    signed = sign_message(req, sk)
    
    assert verify_message(signed) is True
    
    # Change ephemeral field
    # JoinRequest is frozen, so we create a new one with same signature
    modified = JoinRequest(
        node_id=signed.node_id,
        host=signed.host,
        port=signed.port,
        ephemeral=True,
        msg_id=signed.msg_id,
        public_key=signed.public_key,
        signature=signed.signature
    )
    
    # Signature should still be valid because ephemeral is excluded
    assert verify_message(modified) is True

def test_ephemeral_default_false():
    req = JoinRequest(node_id="n1", host="h1", port=9000)
    assert req.ephemeral is False
