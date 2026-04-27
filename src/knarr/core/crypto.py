"""Consolidated cryptography module for Knarr.

All NaCl operations go through this module. Consumers import from here,
not directly from nacl. After C-01, `grep -r "from nacl" src/knarr/`
should return only this file.

Capabilities:
  - Re-exports: SigningKey, VerifyKey, BadSignatureError, SealedBox,
                PublicKey, SecretBox, argon2id, random, HexEncoder
  - X25519 key derivation from Ed25519
  - SealedBox peer encryption / decryption
  - Ed25519 sign helpers
  - TLS context helpers (delegates to mail/tls)
  - C-05: Hybrid multi-recipient encryption (AES-256-GCM + SealedBox key wrap)
"""

import base64
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

# ── NaCl re-exports (all consumers import from here) ──────────────────────────
from nacl.signing import SigningKey, VerifyKey                  # noqa: F401
from nacl.exceptions import BadSignatureError                   # noqa: F401
from nacl.public import SealedBox, PublicKey, PrivateKey        # noqa: F401
from nacl.secret import SecretBox                               # noqa: F401
from nacl.pwhash import argon2id                                # noqa: F401
from nacl.utils import random as nacl_random                    # noqa: F401
from nacl.encoding import HexEncoder                            # noqa: F401
from nacl.bindings import crypto_core_ed25519_is_valid_point    # noqa: F401

logger = logging.getLogger(__name__)
DEFAULT_SAMPLE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


# ── Convenience alias for nacl.utils.random ────────────────────────────────────
random = nacl_random


# ── X25519 derivation ──────────────────────────────────────────────────────────

def derive_x25519_keys(signing_key: SigningKey):
    """Derive X25519 (Curve25519) keys from an Ed25519 SigningKey.

    Returns:
        (x25519_private, x25519_public) — NaCl PrivateKey and PublicKey objects.
    """
    x25519_private = signing_key.to_curve25519_private_key()
    x25519_public = signing_key.verify_key.to_curve25519_public_key()
    logger.debug("CRYPTO_X25519_DERIVE pub=%s", x25519_public.encode().hex()[:16])
    return x25519_private, x25519_public


# ── SealedBox peer encryption ──────────────────────────────────────────────────

def seal_for_peer(data: bytes, peer_x25519_pub_hex: str, trace_id: str = "") -> bytes:
    """Encrypt data for a peer using NaCl SealedBox.

    Args:
        data: Plaintext bytes.
        peer_x25519_pub_hex: Peer's X25519 public key as hex string.
        trace_id: Optional trace ID for structured logging.

    Returns:
        Encrypted ciphertext bytes.
    """
    box = SealedBox(PublicKey(bytes.fromhex(peer_x25519_pub_hex)))
    ciphertext = box.encrypt(data)
    logger.debug("CRYPTO_SEAL peer=%s len=%d trace_id=%s",
                 peer_x25519_pub_hex[:16], len(data), trace_id)
    return ciphertext


def unseal(data: bytes, x25519_private, trace_id: str = "") -> bytes:
    """Decrypt SealedBox ciphertext using node's X25519 private key.

    Args:
        data: Ciphertext bytes.
        x25519_private: NaCl PrivateKey (X25519).
        trace_id: Optional trace ID for structured logging.

    Returns:
        Decrypted plaintext bytes.
    """
    box = SealedBox(x25519_private)
    plaintext = box.decrypt(data)
    logger.debug("CRYPTO_UNSEAL len=%d trace_id=%s", len(data), trace_id)
    return plaintext


# ── Identity-aware encrypt/decrypt ─────────────────────────────────────────────

def encrypt_for_peer_hex(data: bytes, peer_x25519_pub_hex: str,
                         trace_id: str = "") -> bytes:
    """Alias for seal_for_peer — encrypts data using peer's hex-encoded X25519 key."""
    return seal_for_peer(data, peer_x25519_pub_hex, trace_id)


def decrypt_sealed(data: bytes, x25519_private, trace_id: str = "") -> bytes:
    """Alias for unseal — decrypt SealedBox-encrypted data."""
    return unseal(data, x25519_private, trace_id)


# ── Receipt verification (existing, kept in place) ─────────────────────────────

def verify_receipt(receipt_json: str, provider_public_key_hex: str) -> bool:
    """Decode and verify an execution receipt using the provider's Ed25519 public key.

    Args:
        receipt_json: JSON string with {"data": {payload...}, "signature": "<base64>"}
        provider_public_key_hex: Ed25519 public (verify) key as hex string.
    """
    try:
        receipt = json.loads(receipt_json)
        if not isinstance(receipt, dict):
            return False

        data = receipt.get("data")
        signature_b64 = receipt.get("signature")
        if not isinstance(data, dict) or not signature_b64:
            return False

        expected_node_id = hashlib.sha256(bytes.fromhex(provider_public_key_hex)).hexdigest()
        if data.get("provider_node_id") != expected_node_id:
            return False

        payload_bytes = json.dumps(data, sort_keys=True, separators=(',', ':')).encode('utf-8')
        signature_bytes = base64.b64decode(signature_b64)
        verify_key = VerifyKey(bytes.fromhex(provider_public_key_hex))
        verify_key.verify(payload_bytes, signature_bytes)
        return True
    except (json.JSONDecodeError, BadSignatureError, ValueError, TypeError) as e:
        logger.debug("CRYPTO_VERIFY_RECEIPT_FAIL err=%s", e)
        return False


def _sample_timestamp_seconds(value) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError("missing timestamp")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty timestamp")
        try:
            return float(text)
        except ValueError:
            pass
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    raise ValueError("invalid timestamp")


def verify_skill_sample(
    sample: dict,
    rater_pubkey: bytes = None,
    trusted_raters=None,
    sample_max_age_seconds=None,
    now=None,
) -> bool:
    """Verify a skill sample's Ed25519 signature and freshness.

    Used by discovery clients to validate quality-rating evidence embedded in
    a SkillSheet's `samples` field.

    The signed payload is the JSON canonicalization of the sample dict with the
    signature field removed. Canonicalization uses sorted keys and the same
    separators as verify_receipt (``sort_keys=True``, ``separators=(',', ':')``).
    The sample must include a ``timestamp`` field in that signed payload.

    Args:
        sample: A single sample record dict from a SkillSheet's samples list.
                Must contain at minimum: rater_pubkey (64-char hex),
                signature (base64), rater_node_id (64-char hex), and timestamp.
        rater_pubkey: Optional 32-byte Ed25519 verify key. If provided, the
                verification uses this key and the sample's embedded rater_pubkey
                is checked for consistency. If None, the sample's embedded
                rater_pubkey is used (client-trust-list pattern).
        trusted_raters: Optional set of trusted rater public keys or node IDs.
                If None, any valid signer is accepted.
        sample_max_age_seconds: Freshness window. Defaults to 7 days.
        now: Optional epoch seconds override for deterministic tests.

    Returns:
        True if the signature is valid, fresh, and from an allowed rater.
        False on any error (malformed sample, decoding failure, signature mismatch,
        stale timestamp, untrusted rater, or rater_node_id ↔ sha256(rater_pubkey)
        consistency failure).
    """
    try:
        if not isinstance(sample, dict):
            return False
        signature_b64 = sample.get("signature")
        embedded_pubkey_hex = sample.get("rater_pubkey")
        if not signature_b64 or not embedded_pubkey_hex:
            return False
        if "timestamp" not in sample:
            logger.warning("CRYPTO_VERIFY_SAMPLE_MISSING_TIMESTAMP rater=%s", str(embedded_pubkey_hex)[:16])
            return False

        # Consistency: caller-provided pubkey (if any) must match the embedded one
        if rater_pubkey is not None:
            provided_hex = rater_pubkey.hex() if isinstance(rater_pubkey, (bytes, bytearray)) else str(rater_pubkey)
            if provided_hex.lower() != embedded_pubkey_hex.lower():
                logger.debug(
                    "CRYPTO_VERIFY_SAMPLE_FAIL provided_pubkey=%s embedded=%s",
                    provided_hex[:16], embedded_pubkey_hex[:16],
                )
                return False

        rater_node_id = sample.get("rater_node_id", "")
        if trusted_raters is not None:
            trusted = {
                (item.hex() if isinstance(item, (bytes, bytearray)) else str(item)).lower()
                for item in trusted_raters
            }
            if embedded_pubkey_hex.lower() not in trusted and str(rater_node_id).lower() not in trusted:
                logger.debug("CRYPTO_VERIFY_SAMPLE_UNTRUSTED rater=%s", embedded_pubkey_hex[:16])
                return False

        max_age = DEFAULT_SAMPLE_MAX_AGE_SECONDS if sample_max_age_seconds is None else float(sample_max_age_seconds)
        timestamp_seconds = _sample_timestamp_seconds(sample.get("timestamp"))
        current_time = time.time() if now is None else float(now)
        if timestamp_seconds > current_time + 300:
            logger.debug("CRYPTO_VERIFY_SAMPLE_FUTURE_TIMESTAMP rater=%s", str(embedded_pubkey_hex)[:16])
            return False
        if current_time - timestamp_seconds > max_age:
            logger.debug(
                "CRYPTO_VERIFY_SAMPLE_STALE age=%.3f max_age=%.3f rater=%s",
                current_time - timestamp_seconds, max_age, embedded_pubkey_hex[:16],
            )
            return False

        # Canonicalize the sample without the signature field
        unsigned = {k: v for k, v in sample.items() if k != "signature"}
        payload_bytes = json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode('utf-8')

        # Signature may be base64 OR raw hex (matches how raters serialize)
        try:
            signature_bytes = base64.b64decode(signature_b64)
            if len(signature_bytes) != 64:
                # Not valid base64 Ed25519 — try hex
                signature_bytes = bytes.fromhex(signature_b64)
        except Exception:
            signature_bytes = bytes.fromhex(signature_b64)

        verify_key = VerifyKey(bytes.fromhex(embedded_pubkey_hex))
        verify_key.verify(payload_bytes, signature_bytes)

        # Consistency: rater_node_id MUST be sha256(rater_pubkey) — mirrors
        # verify_receipt's provider_node_id check
        expected_node_id = hashlib.sha256(bytes.fromhex(embedded_pubkey_hex)).hexdigest()
        if rater_node_id != expected_node_id:
            logger.debug(
                "CRYPTO_VERIFY_SAMPLE_FAIL rater_node_id=%s expected=%s",
                rater_node_id[:16], expected_node_id[:16],
            )
            return False

        return True
    except (json.JSONDecodeError, BadSignatureError, ValueError, TypeError) as e:
        logger.debug("CRYPTO_VERIFY_SAMPLE_FAIL err=%s", e)
        return False


# ── TLS context helpers (delegates to mail/tls) ────────────────────────────────

def create_server_tls_context(cert_path: str, key_path: str):
    """Create TLS server SSLContext. Returns None if cert/key paths are missing."""
    import ssl
    if not cert_path or not key_path:
        return None
    if not os.path.exists(cert_path) or not os.path.exists(key_path):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_path, key_path)
    logger.debug("CRYPTO_TLS_SERVER_CTX cert=%s", cert_path)
    return ctx


def create_client_tls_context():
    """Create TLS client SSLContext for peer connections.

    Uses CERT_NONE — wire is encrypted, peer identity is verified by
    Ed25519 message signatures (already implemented in the protocol).
    """
    import ssl
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    logger.debug("CRYPTO_TLS_CLIENT_CTX created")
    return ctx


def get_tls_peer_cert_fingerprint(ssl_object) -> str:
    """Return the SHA-256 fingerprint of the peer TLS certificate, or ''."""
    if ssl_object is None:
        return ""
    try:
        cert_bytes = ssl_object.getpeercert(binary_form=True)
    except Exception:
        return ""
    if not cert_bytes:
        return ""
    return hashlib.sha256(cert_bytes).hexdigest()


def ensure_node_tls_cert(config: dict, data_dir: str, node_id: str,
                          node_key_bytes: bytes) -> tuple:
    """Generate node TLS cert if not present. Returns (cert_path, key_path).

    Args:
        config: Node configuration dict.
        data_dir: Directory for cert storage.
        node_id: Node ID hex string (for CN).
        node_key_bytes: Raw Ed25519 seed bytes.
    """
    from ..mail.tls import generate_tls_cert, resolve_cert_paths
    cert_path, key_path = resolve_cert_paths(config, data_dir)
    network_cfg = config.get("network", {})
    custom_tls = "tls_cert" in network_cfg or "tls_key" in network_cfg
    if not custom_tls and (not os.path.exists(cert_path) or not os.path.exists(key_path)):
        if node_key_bytes:
            generate_tls_cert(node_key_bytes, node_id, data_dir)
            logger.info("CRYPTO_TLS_CERT_GENERATED node_id=%s", node_id[:16])
    return cert_path, key_path


# ── C-05: Hybrid multi-recipient encryption ────────────────────────────────────
#
# Scheme:
#   1. Generate a random 256-bit AES session key.
#   2. Encrypt plaintext with AES-256-GCM (random 96-bit nonce).
#   3. Wrap the session key for each recipient using their X25519 public key
#      via NaCl SealedBox.
#
# Wire format returned by hybrid_encrypt:
#   {
#     "ciphertext": "<base64 of AES-GCM output>",
#     "nonce":      "<base64 of 12-byte nonce>",
#     "recipient_keys": {
#       "<x25519_pub_hex>": "<base64 of SealedBox-wrapped session key>",
#       ...
#     }
#   }


def _hybrid_recipient_aad(recipient_x25519_pub_hexes: list[str]) -> bytes:
    """Canonical AES-GCM AAD for the wrapped recipient set.

    Adversary #5 fix (v0.56.0): normalize to lowercase before sorting.
    Without the normalization, `"ABCD"` and `"abcd"` produce different AAD
    even though they represent the same hex key — logically-identical
    recipient sets would fail AAD validation on decrypt.
    """
    recipients = sorted({str(pub_hex).lower() for pub_hex in recipient_x25519_pub_hexes if pub_hex})
    return json.dumps(recipients, separators=(",", ":")).encode("utf-8")

def hybrid_encrypt(plaintext: bytes, recipient_x25519_pub_hexes: list,
                   trace_id: str = "") -> dict:
    """Encrypt plaintext for multiple recipients.

    Each recipient can independently decrypt using their X25519 private key.

    Args:
        plaintext: Data to encrypt.
        recipient_x25519_pub_hexes: List of X25519 public keys as hex strings.
        trace_id: Optional trace ID for logging.

    Returns:
        Payload dict suitable for JSON serialization.
    """
    if not recipient_x25519_pub_hexes:
        raise ValueError("At least one recipient required for hybrid encryption")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    recipient_x25519_pub_hexes = sorted({
        (pub_hex.hex() if isinstance(pub_hex, (bytes, bytearray)) else str(pub_hex)).lower()
        for pub_hex in recipient_x25519_pub_hexes if pub_hex
    })
    session_key = os.urandom(32)
    nonce = os.urandom(12)
    aesgcm = AESGCM(session_key)
    aad = _hybrid_recipient_aad(recipient_x25519_pub_hexes)
    ciphertext = aesgcm.encrypt(nonce, plaintext, aad)

    recipient_keys = {}
    for pub_hex in recipient_x25519_pub_hexes:
        box = SealedBox(PublicKey(bytes.fromhex(pub_hex)))
        wrapped = box.encrypt(session_key)
        recipient_keys[pub_hex] = base64.b64encode(wrapped).decode()

    logger.debug("CRYPTO_HYBRID_ENCRYPT recipients=%d plaintext_len=%d trace_id=%s",
                 len(recipient_x25519_pub_hexes), len(plaintext), trace_id)
    return {
        "ciphertext": base64.b64encode(ciphertext).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "recipient_keys": recipient_keys,
    }


def hybrid_decrypt(payload: dict, x25519_private, trace_id: str = "") -> bytes:
    """Decrypt a hybrid-encrypted payload using the recipient's X25519 private key.

    Args:
        payload: Dict produced by hybrid_encrypt.
        x25519_private: NaCl PrivateKey (X25519) of the recipient.
        trace_id: Optional trace ID for logging.

    Returns:
        Decrypted plaintext bytes.

    Raises:
        ValueError: If no wrapped key found for this recipient.
        nacl.exceptions.CryptoError: If decryption fails.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    pub_hex = x25519_private.public_key.encode().hex()
    recipient_keys = payload.get("recipient_keys", {})
    wrapped_b64 = recipient_keys.get(pub_hex)
    if not wrapped_b64:
        raise ValueError(f"No wrapped key found for recipient {pub_hex[:16]}...")

    box = SealedBox(x25519_private)
    session_key = box.decrypt(base64.b64decode(wrapped_b64))

    nonce = base64.b64decode(payload["nonce"])
    ciphertext = base64.b64decode(payload["ciphertext"])
    aesgcm = AESGCM(session_key)
    aad = _hybrid_recipient_aad(list(recipient_keys.keys()))
    plaintext = aesgcm.decrypt(nonce, ciphertext, aad)

    logger.debug("CRYPTO_HYBRID_DECRYPT pub=%s trace_id=%s", pub_hex[:16], trace_id)
    return plaintext
