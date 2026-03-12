import base64
import json

from nacl.signing import SigningKey

from knarr.core.crypto import verify_receipt


def _receipt(signing_key: SigningKey, payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signed = signing_key.sign(canonical)
    return json.dumps({
        "data": payload,
        "signature": base64.b64encode(signed.signature).decode("ascii"),
    })


def test_verify_receipt_accepts_valid_signature():
    signing_key = SigningKey.generate()
    payload = {"provider_node_id": signing_key.verify_key.encode().hex()}
    payload["provider_node_id"] = __import__("hashlib").sha256(bytes.fromhex(signing_key.verify_key.encode().hex())).hexdigest()
    assert verify_receipt(_receipt(signing_key, payload), signing_key.verify_key.encode().hex()) is True


def test_verify_receipt_rejects_invalid_json():
    assert verify_receipt("{", "00" * 32) is False


def test_verify_receipt_rejects_non_dict_receipt():
    assert verify_receipt("[]", "00" * 32) is False


def test_verify_receipt_rejects_missing_signature():
    signing_key = SigningKey.generate()
    payload = {"provider_node_id": "a" * 64}
    assert verify_receipt(json.dumps({"data": payload}), signing_key.verify_key.encode().hex()) is False


def test_verify_receipt_rejects_missing_data():
    signing_key = SigningKey.generate()
    assert verify_receipt(json.dumps({"signature": "abc"}), signing_key.verify_key.encode().hex()) is False


def test_verify_receipt_rejects_wrong_provider_node_id():
    signing_key = SigningKey.generate()
    payload = {"provider_node_id": "f" * 64}
    assert verify_receipt(_receipt(signing_key, payload), signing_key.verify_key.encode().hex()) is False


def test_verify_receipt_rejects_wrong_public_key():
    signing_key = SigningKey.generate()
    other = SigningKey.generate()
    payload = {
        "provider_node_id": __import__("hashlib").sha256(signing_key.verify_key.encode()).hexdigest(),
    }
    assert verify_receipt(_receipt(signing_key, payload), other.verify_key.encode().hex()) is False


def test_verify_receipt_rejects_tampered_payload():
    signing_key = SigningKey.generate()
    payload = {
        "provider_node_id": __import__("hashlib").sha256(signing_key.verify_key.encode()).hexdigest(),
    }
    receipt = json.loads(_receipt(signing_key, payload))
    receipt["data"]["extra"] = 1
    assert verify_receipt(json.dumps(receipt), signing_key.verify_key.encode().hex()) is False


def test_verify_receipt_rejects_malformed_base64_signature():
    signing_key = SigningKey.generate()
    payload = {
        "provider_node_id": __import__("hashlib").sha256(signing_key.verify_key.encode()).hexdigest(),
    }
    receipt = json.dumps({"data": payload, "signature": "***"})
    assert verify_receipt(receipt, signing_key.verify_key.encode().hex()) is False


def test_verify_receipt_rejects_invalid_public_key_hex():
    signing_key = SigningKey.generate()
    payload = {
        "provider_node_id": __import__("hashlib").sha256(signing_key.verify_key.encode()).hexdigest(),
    }
    assert verify_receipt(_receipt(signing_key, payload), "not-hex") is False
