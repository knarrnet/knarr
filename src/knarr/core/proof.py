"""
W3C Data Integrity proof creation and verification.
Implements eddsa-jcs-2022 cryptosuite (RFC 8785 canonicalization + Ed25519).

Layer 2 of the two-layer receipt architecture:
- Knows about SIGNING, not about document shapes.
- Document-type-agnostic: signs any JSON dict identically.

Spec: https://www.w3.org/TR/vc-di-eddsa/#eddsa-jcs-2022
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

from .crypto import BadSignatureError, SigningKey, VerifyKey

from . import rfc8785

logger = logging.getLogger(__name__)

# Base58 Bitcoin alphabet (no checksum variant)
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def sign_document(
    document: dict,
    private_key: SigningKey,
    verification_method: str,
    proof_purpose: str = "assertionMethod",
    created: Optional[str] = None,
) -> dict:
    """Sign a JSON document per eddsa-jcs-2022.

    Returns a NEW dict: document + embedded proof object.
    Does NOT mutate the input.

    Algorithm (W3C DI Section 3.3):
    1. Build proof_options (without proofValue)
    2. canonical_proof = JCS(proof_options)
    3. canonical_doc   = JCS(document)
    4. hash_data = SHA-256(canonical_proof) || SHA-256(canonical_doc)  [64 bytes]
    5. signature = Ed25519_Sign(private_key, hash_data)
    6. proof_options["proofValue"] = multibase_base58btc(signature)
    7. document["proof"] = proof_options
    """
    # 1. Proof options
    proof_options = {
        "type": "DataIntegrityProof",
        "cryptosuite": "eddsa-jcs-2022",
        "verificationMethod": verification_method,
        "proofPurpose": proof_purpose,
        "created": created or _iso_now(),
    }

    # 2-3. Canonicalize
    # Strip any existing proof before hashing — verify_document pops proof
    # before canonicalizing, so the signed and verified bytes must match. (F-15)
    doc_without_proof = {k: v for k, v in document.items() if k != "proof"}
    canonical_proof = rfc8785.dumps(proof_options)
    canonical_doc = rfc8785.dumps(doc_without_proof)

    # 4. Double-hash: proof_config FIRST, then document
    hash_data = hashlib.sha256(canonical_proof).digest() + hashlib.sha256(canonical_doc).digest()

    # 5. Sign
    signed = private_key.sign(hash_data)

    # 6. Multibase base58btc encoding (z prefix)
    proof_options["proofValue"] = "z" + _base58btc_encode(signed.signature)

    # 7. Embed proof in document copy
    secured = dict(document)
    secured["proof"] = proof_options
    return secured


def verify_document(
    secured_document: dict,
    public_key: VerifyKey,
) -> bool:
    """Verify a signed document per eddsa-jcs-2022.

    Returns True if signature is valid, False otherwise.
    Never raises on invalid signatures — returns False.
    """
    try:
        # 1. Extract and strip proof
        doc = dict(secured_document)
        proof = dict(doc.pop("proof"))
        proof_value = proof.pop("proofValue")

        # 2. Decode signature (strip 'z' multibase prefix)
        if not isinstance(proof_value, str) or not proof_value.startswith("z"):
            return False
        signature = _base58btc_decode(proof_value[1:])
        if len(signature) != 64:  # Ed25519 signatures are always 64 bytes
            return False

        # 3. Canonicalize
        canonical_proof = rfc8785.dumps(proof)
        canonical_doc = rfc8785.dumps(doc)

        # 4. Double-hash: proof_config FIRST
        hash_data = hashlib.sha256(canonical_proof).digest() + hashlib.sha256(canonical_doc).digest()

        # 5. Verify
        public_key.verify(hash_data, signature)
        return True
    except (BadSignatureError, KeyError, ValueError, TypeError, AttributeError):
        return False


def _iso_now() -> str:
    """UTC ISO 8601 timestamp with millisecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _base58btc_encode(data: bytes) -> str:
    """Base58 Bitcoin encoding (no checksum)."""
    n = int.from_bytes(data, "big")
    result = []
    while n > 0:
        n, r = divmod(n, 58)
        result.append(_B58_ALPHABET[r])
    # Preserve leading zero bytes
    for b in data:
        if b == 0:
            result.append(_B58_ALPHABET[0])
        else:
            break
    return "".join(reversed(result))


def _base58btc_decode(s: str) -> bytes:
    """Base58 Bitcoin decoding."""
    n = 0
    for c in s:
        n = n * 58 + _B58_ALPHABET.index(c)
    # Count leading '1's (zero bytes in base58)
    pad = 0
    for c in s:
        if c == "1":
            pad += 1
        else:
            break
    result = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + result
