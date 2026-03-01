"""Validation schemas for commerce documents."""
import math


def _is_finite(v) -> bool:
    """Check that a numeric value is finite (not NaN, not inf)."""
    return isinstance(v, (int, float)) and math.isfinite(v)

def validate_receipt(body: dict) -> tuple[bool, str | None]:
    """Validate a knarr/commerce/receipt message body."""
    if body.get("type") != "knarr/commerce/receipt":
        return False, "wrong type"
    for field in ["task_id", "status", "timestamp"]:
        if field not in body:
            return False, f"missing required field: {field}"
    if body["status"] not in ("accepted", "rejected"):
        return False, f"invalid status: {body['status']}"
    qr = body.get("quality_rating")
    if qr is not None and (not isinstance(qr, int) or qr < 1 or qr > 5):
        return False, f"quality_rating must be int 1-5, got {qr}"
    return True, None


def validate_credit_note(body: dict) -> tuple[bool, str | None]:
    """Validate a knarr/commerce/credit_note message body."""
    if body.get("type") != "knarr/commerce/credit_note":
        return False, "wrong type"
    for field in ["amount", "reason", "timestamp", "references"]:
        if field not in body:
            return False, f"missing required field: {field}"
    refs = body["references"]
    if not isinstance(refs, dict) or "task_id" not in refs:
        return False, "references must contain task_id"
    if not _is_finite(body["amount"]) or body["amount"] <= 0:
        return False, f"amount must be > 0, got {body['amount']}"
    if body["reason"] not in ("quality_rejection", "provider_unable", "partial_refund", "other"):
        return False, f"invalid reason: {body['reason']}"
    initiated_by = body.get("initiated_by")
    if initiated_by is not None and initiated_by not in ("customer", "provider"):
        return False, f"invalid initiated_by: {initiated_by}"
    return True, None


def validate_signed_credit_note(body: dict) -> tuple[bool, str | None]:
    """Validate a v0.32.0 signed credit note artifact (from receipts.py).

    This is DIFFERENT from validate_credit_note() which validates unsigned
    commerce messages. The signed note uses type="credit_note" (not
    "knarr/commerce/credit_note") and has Ed25519 signature fields.
    """
    if body.get("type") != "credit_note":
        return False, "wrong type (expected 'credit_note')"
    if body.get("version") != 1:
        return False, f"unsupported version: {body.get('version')}"
    for field in ["note_type", "amount", "issuer", "recipient", "reference",
                   "description", "timestamp", "signature"]:
        if field not in body:
            return False, f"missing required field: {field}"
    if body["note_type"] not in ("debit", "credit", "zero"):
        return False, f"invalid note_type: {body['note_type']}"
    if not _is_finite(body["amount"]) or body["amount"] < 0:
        return False, f"amount must be >= 0 and finite, got {body['amount']}"
    if not isinstance(body["issuer"], str) or len(body["issuer"]) != 64:
        return False, f"issuer must be 64-char hex"
    if not isinstance(body["recipient"], str) or len(body["recipient"]) != 64:
        return False, f"recipient must be 64-char hex"
    return True, None


def validate_settle_request(body: dict) -> tuple[bool, str | None]:
    """Validate a knarr/commerce/settle_request message body."""
    if body.get("type") != "knarr/commerce/settle_request":
        return False, "wrong type"
    for field in ["current_balance", "credit_limit", "provider_wallet", "timestamp"]:
        if field not in body:
            return False, f"missing required field: {field}"
    # Numeric validation
    for nf in ["current_balance", "credit_limit", "timestamp"]:
        v = body[nf]
        if not isinstance(v, (int, float)) or not _is_finite(v):
            return False, f"{nf} must be a finite number, got {v!r}"
    action = body.get("requested_action")
    if action is not None and action not in ("settle_to_zero", "settle_partial"):
        return False, f"invalid requested_action: {action}"
    wallet = body.get("provider_wallet")
    if not isinstance(wallet, str) or not (32 <= len(wallet) <= 44):
        return False, f"invalid provider_wallet length: {wallet}"
    return True, None


def validate_settlement_confirmation(body: dict) -> tuple[bool, str | None]:
    """Validate a knarr/commerce/settlement_confirmation message body."""
    if body.get("type") != "knarr/commerce/settlement_confirmation":
        return False, "wrong type"
    for field in ["tx_hash", "amount_settled", "timestamp"]:
        if field not in body:
            return False, f"missing required field: {field}"
    # Numeric validation
    for nf in ["amount_settled", "timestamp"]:
        v = body[nf]
        if not isinstance(v, (int, float)) or not _is_finite(v):
            return False, f"{nf} must be a finite number, got {v!r}"
    if body["amount_settled"] <= 0:
        return False, f"amount_settled must be > 0, got {body['amount_settled']}"
    tx = body.get("tx_hash")
    if not isinstance(tx, str) or not (85 <= len(tx) <= 90):
        return False, f"invalid tx_hash length: {tx}"
    return True, None


def validate_tab_reminder(body: dict) -> tuple[bool, str | None]:
    """Validate a knarr/commerce/tab_reminder message body."""
    if body.get("type") != "knarr/commerce/tab_reminder":
        return False, "wrong type"
    for field in ["current_balance", "credit_limit", "utilization_pct", "timestamp"]:
        if field not in body:
            return False, f"missing required field: {field}"
    # Numeric validation
    for nf in ["current_balance", "credit_limit", "utilization_pct", "timestamp"]:
        v = body[nf]
        if not isinstance(v, (int, float)) or not _is_finite(v):
            return False, f"{nf} must be a finite number, got {v!r}"
    return True, None
