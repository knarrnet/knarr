"""Validation schemas for commerce documents."""

from __future__ import annotations

import math
from typing import Iterable


def _is_finite(v) -> bool:
    """Check that a numeric value is finite (not NaN, not inf)."""
    return isinstance(v, (int, float)) and math.isfinite(v)


def _require_fields(body: dict, fields: Iterable[str]) -> tuple[bool, str | None]:
    for field in fields:
        if field not in body:
            return False, f"missing required field: {field}"
    return True, None


def _validate_token(body: dict) -> tuple[bool, str | None]:
    token = body.get("token")
    if not isinstance(token, str) or not token.strip():
        return False, "token must be a non-empty string"
    return True, None


def validate_receipt(body: dict) -> tuple[bool, str | None]:
    """Validate a knarr/commerce/receipt message body."""
    if body.get("type") != "knarr/commerce/receipt":
        return False, "wrong type"
    ok, err = _require_fields(body, ["task_id", "status", "timestamp"])
    if not ok:
        return ok, err
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
    ok, err = _require_fields(body, ["amount", "reason", "timestamp", "references"])
    if not ok:
        return ok, err
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


def validate_settle_request(body: dict) -> tuple[bool, str | None]:
    """Validate a knarr/commerce/settle_request message body."""
    if body.get("type") != "knarr/commerce/settle_request":
        return False, "wrong type"
    ok, err = _require_fields(body, ["current_balance", "credit_limit", "provider_wallet", "timestamp"])
    if not ok:
        return ok, err
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
    ok, err = _require_fields(body, ["tx_hash", "amount_settled", "timestamp"])
    if not ok:
        return ok, err
    for nf in ["amount_settled", "timestamp"]:
        v = body[nf]
        if not isinstance(v, (int, float)) or not _is_finite(v):
            return False, f"{nf} must be a finite number, got {v!r}"
    if body["amount_settled"] <= 0:
        return False, f"amount_settled must be > 0, got {body['amount_settled']}"
    tx = body.get("tx_hash")
    if not isinstance(tx, str) or not (1 <= len(tx) <= 128):
        return False, f"invalid tx_hash length: {tx}"
    return True, None


def validate_tab_reminder(body: dict) -> tuple[bool, str | None]:
    """Validate a knarr/commerce/tab_reminder message body."""
    if body.get("type") != "knarr/commerce/tab_reminder":
        return False, "wrong type"
    ok, err = _require_fields(body, ["current_balance", "credit_limit", "utilization_pct", "timestamp"])
    if not ok:
        return ok, err
    for nf in ["current_balance", "credit_limit", "utilization_pct", "timestamp"]:
        v = body[nf]
        if not isinstance(v, (int, float)) or not _is_finite(v):
            return False, f"{nf} must be a finite number, got {v!r}"
    return True, None


def validate_payment_received(body: dict) -> tuple[bool, str | None]:
    ok, err = _require_fields(
        body,
        ["chain_id", "tx_hash", "tx_index", "from_address", "to_address", "amount", "denom", "decimals", "confirmation"],
    )
    if not ok:
        return ok, err
    if not _is_finite(body["amount"]):
        return False, "amount must be finite"
    if not isinstance(body["decimals"], int) or body["decimals"] < 0:
        return False, "decimals must be an integer >= 0"
    return True, None


def validate_payment_finalized(body: dict) -> tuple[bool, str | None]:
    ok, err = _require_fields(
        body,
        ["chain_id", "tx_hash", "amount", "denom", "original_receipt_id", "finality"],
    )
    if not ok:
        return ok, err
    if not _is_finite(body["amount"]):
        return False, "amount must be finite"
    return True, None


def validate_payment_executed(body: dict) -> tuple[bool, str | None]:
    ok, err = _require_fields(
        body,
        ["chain_id", "tx_hash", "from_address", "to_address", "amount", "denom", "decimals", "settlement_ref", "finality"],
    )
    if not ok:
        return ok, err
    if not _is_finite(body["amount"]):
        return False, "amount must be finite"
    if not isinstance(body["decimals"], int) or body["decimals"] < 0:
        return False, "decimals must be an integer >= 0"
    return True, None


def validate_wallet_transfer(body: dict) -> tuple[bool, str | None]:
    ok, err = _require_fields(
        body,
        ["chain_id", "tx_hash", "from_address", "to_address", "amount", "denom", "decimals", "transfer_type"],
    )
    if not ok:
        return ok, err
    if not _is_finite(body["amount"]):
        return False, "amount must be finite"
    if not isinstance(body["decimals"], int) or body["decimals"] < 0:
        return False, "decimals must be an integer >= 0"
    return True, None


def validate_wallet_withdrawal(body: dict) -> tuple[bool, str | None]:
    ok, err = _require_fields(
        body,
        ["chain_id", "tx_hash", "from_address", "to_address", "amount", "denom", "decimals"],
    )
    if not ok:
        return ok, err
    if not _is_finite(body["amount"]):
        return False, "amount must be finite"
    if not isinstance(body["decimals"], int) or body["decimals"] < 0:
        return False, "decimals must be an integer >= 0"
    return True, None


def validate_configuration_order(body: dict) -> tuple[bool, str | None]:
    ok, err = _require_fields(body, ["target", "operation", "changes"])
    if not ok:
        return ok, err
    if not isinstance(body["changes"], dict):
        return False, "changes must be an object"
    return True, None


def validate_punchhole_card(body: dict) -> tuple[bool, str | None]:
    ok, err = _require_fields(body, ["for_node", "for_access_level", "available", "not_available"])
    if not ok:
        return ok, err
    if not isinstance(body["available"], list) or not isinstance(body["not_available"], list):
        return False, "available and not_available must be lists"
    return True, None


def validate_cache_object(body: dict) -> tuple[bool, str | None]:
    ok, err = _require_fields(body, ["object_key", "data", "granularity"])
    if not ok:
        return ok, err
    if not isinstance(body["data"], dict):
        return False, "data must be an object"
    if not isinstance(body["granularity"], dict):
        return False, "granularity must be an object"
    return True, None


def validate_netting_reconcile(body: dict) -> tuple[bool, str | None]:
    ok, err = _require_fields(
        body,
        ["netting_id", "identity", "counterparty", "proposed_net", "receipt_count", "chain_id", "token"],
    )
    if not ok:
        return ok, err
    tok_ok, tok_err = _validate_token(body)
    if not tok_ok:
        return tok_ok, tok_err
    if not _is_finite(body["proposed_net"]):
        return False, "proposed_net must be finite"
    if not isinstance(body["receipt_count"], int) or body["receipt_count"] < 0:
        return False, "receipt_count must be an integer >= 0"
    return True, None


def validate_netting_proposal(body: dict) -> tuple[bool, str | None]:
    ok, err = _require_fields(
        body,
        ["netting_id", "identity", "counterparty", "settlement_amount", "chain_id", "token", "target_address", "deadline"],
    )
    if not ok:
        return ok, err
    tok_ok, tok_err = _validate_token(body)
    if not tok_ok:
        return tok_ok, tok_err
    if not _is_finite(body["settlement_amount"]) or body["settlement_amount"] <= 0:
        return False, "settlement_amount must be > 0"
    if not isinstance(body["target_address"], str) or not body["target_address"].strip():
        return False, "target_address must be a non-empty string"
    if not isinstance(body["deadline"], str) or not body["deadline"].strip():
        return False, "deadline must be a non-empty string"
    return True, None


def validate_netting_acceptance(body: dict) -> tuple[bool, str | None]:
    ok, err = _require_fields(
        body,
        ["netting_id", "proposal_ref", "identity", "counterparty", "accepted_amount", "source_address", "token"],
    )
    if not ok:
        return ok, err
    tok_ok, tok_err = _validate_token(body)
    if not tok_ok:
        return tok_ok, tok_err
    if not _is_finite(body["accepted_amount"]) or body["accepted_amount"] <= 0:
        return False, "accepted_amount must be > 0"
    if not isinstance(body["source_address"], str) or not body["source_address"].strip():
        return False, "source_address must be a non-empty string"
    return True, None


def validate_netting_executed(body: dict) -> tuple[bool, str | None]:
    ok, err = _require_fields(
        body,
        ["netting_id", "acceptance_ref", "identity", "counterparty", "tx_hash", "chain_id", "amount", "token"],
    )
    if not ok:
        return ok, err
    tok_ok, tok_err = _validate_token(body)
    if not tok_ok:
        return tok_ok, tok_err
    if not isinstance(body["tx_hash"], str) or not body["tx_hash"].strip():
        return False, "tx_hash must be a non-empty string"
    if not _is_finite(body["amount"]) or body["amount"] <= 0:
        return False, "amount must be > 0"
    return True, None
