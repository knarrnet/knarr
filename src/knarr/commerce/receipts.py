"""Credit note generation for v0.32.0 receipts.

The receipt IS the transaction; the ledger is derived.
Every billable skill execution produces one signed credit note.

Design principles:
- Canonical JSON (sort_keys, minimal separators) for deterministic signatures.
- Ed25519 signature over the canonical payload (excluding the signature field).
- parent_hash is None in this sprint — reserved for future refund chains.
"""
import base64
import json
import logging
import math
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

VALID_NOTE_TYPES = {"debit", "credit", "zero"}


def create_credit_note(
    note_type: str,
    amount: float,
    issuer: str,
    recipient: str,
    reference: str,
    description: str,
    signing_key,
    parent_hash: Optional[str] = None,
) -> str:
    """Create a signed credit note. Returns canonical JSON string.

    Args:
        note_type:    "debit", "credit", or "zero"
        amount:       Credits charged. Must be >= 0 and finite.
        issuer:       Node public key hex (the provider — signs the note).
        recipient:    Counterparty public key hex (the consumer).
        reference:    Job ID this note refers to.
        description:  Human-readable label e.g. "skill:web-search execution".
        signing_key:  nacl Ed25519 SigningKey.
        parent_hash:  Always None in this sprint; reserved for refund chains.

    Returns:
        Canonical JSON string including signature field.

    Raises:
        ValueError: if note_type or amount are invalid.
    """
    if note_type not in VALID_NOTE_TYPES:
        raise ValueError(f"note_type must be one of {VALID_NOTE_TYPES}, got {note_type!r}")
    if not (math.isfinite(amount) and amount >= 0):
        raise ValueError(f"amount must be finite and >= 0, got {amount!r}")

    timestamp = datetime.now(timezone.utc).isoformat()

    payload = {
        "type": "credit_note",
        "version": 1,
        "note_type": note_type,
        "amount": amount,
        "unit": "credits",
        "issuer": issuer,
        "recipient": recipient,
        "timestamp": timestamp,
        "reference": reference,
        "parent_hash": parent_hash,
        "description": description,
    }

    # Canonical JSON — deterministic key ordering and minimal whitespace
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode("utf-8")
    raw_sig = signing_key.sign(canonical).signature
    sig_b64 = base64.b64encode(raw_sig).decode("ascii")

    payload["signature"] = sig_b64
    result = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    log.debug(f"CREDIT_NOTE_CREATED note_type={note_type} amount={amount} ref={reference[:8]}")
    return result


def verify_credit_note(note_json: str) -> bool:
    """Verify the Ed25519 signature on a credit note.

    Returns True if the signature is valid, False otherwise.
    """
    try:
        from nacl.signing import VerifyKey
        note = json.loads(note_json)
        sig_b64 = note.get("signature")
        if not sig_b64:
            return False
        sig = base64.b64decode(sig_b64)

        # Re-build payload without signature field
        payload = {k: v for k, v in note.items() if k != "signature"}
        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode("utf-8")

        issuer_hex = note.get("issuer", "")
        vk = VerifyKey(bytes.fromhex(issuer_hex))
        vk.verify(canonical, sig)
        return True
    except Exception:
        return False
