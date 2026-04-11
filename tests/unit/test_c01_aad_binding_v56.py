import base64
from copy import deepcopy
import json

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from knarr.core.crypto import (
    SealedBox,
    SigningKey,
    _hybrid_recipient_aad,
    derive_x25519_keys,
    hybrid_decrypt,
    hybrid_encrypt,
)


def _two_recipient_payload():
    sk1 = SigningKey.generate()
    sk2 = SigningKey.generate()
    priv1, pub1 = derive_x25519_keys(sk1)
    _priv2, pub2 = derive_x25519_keys(sk2)
    payload = hybrid_encrypt(b"bound recipients", [pub1.encode().hex(), pub2.encode().hex()])
    return priv1, pub1.encode().hex(), pub2.encode().hex(), payload


def test_hybrid_recipient_aad_is_sorted_compact_json():
    pub_a = "a" * 64
    pub_b = "b" * 64

    assert _hybrid_recipient_aad([pub_b, pub_a, pub_b, ""]) == json.dumps(
        [pub_a, pub_b],
        separators=(",", ":"),
    ).encode("utf-8")


def test_hybrid_encrypt_emits_sorted_unique_recipient_keys():
    sk1 = SigningKey.generate()
    sk2 = SigningKey.generate()
    _priv1, pub1 = derive_x25519_keys(sk1)
    _priv2, pub2 = derive_x25519_keys(sk2)

    payload = hybrid_encrypt(b"bound recipients", [pub2.encode().hex(), pub1.encode().hex(), pub2.encode().hex()])

    assert list(payload["recipient_keys"].keys()) == sorted({pub1.encode().hex(), pub2.encode().hex()})


def test_hybrid_decrypt_fails_when_recipient_set_gains_entry():
    priv1, _pub1_hex, _pub2_hex, payload = _two_recipient_payload()
    tampered = deepcopy(payload)
    tampered["recipient_keys"]["f" * 64] = tampered["recipient_keys"][next(iter(tampered["recipient_keys"]))]

    with pytest.raises(InvalidTag):
        hybrid_decrypt(tampered, priv1)


def test_hybrid_decrypt_fails_when_recipient_set_loses_entry():
    priv1, pub1_hex, pub2_hex, payload = _two_recipient_payload()
    tampered = deepcopy(payload)
    del tampered["recipient_keys"][pub2_hex]
    assert pub1_hex in tampered["recipient_keys"]

    with pytest.raises(InvalidTag):
        hybrid_decrypt(tampered, priv1)


def test_hybrid_ciphertext_rejects_reordered_recipient_aad():
    priv1, pub1_hex, pub2_hex, payload = _two_recipient_payload()
    session_key = SealedBox(priv1).decrypt(base64.b64decode(payload["recipient_keys"][pub1_hex]))
    nonce = base64.b64decode(payload["nonce"])
    ciphertext = base64.b64decode(payload["ciphertext"])
    reordered = list(reversed(sorted([pub1_hex, pub2_hex])))

    assert reordered != sorted([pub1_hex, pub2_hex])
    with pytest.raises(InvalidTag):
        AESGCM(session_key).decrypt(
            nonce,
            ciphertext,
            json.dumps(reordered, separators=(",", ":")).encode("utf-8"),
        )
