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


# ── v0.37.0: BCW Document Validators ─────────────────────────────────


def _validate_chain_tx(body: dict, required: list[str]) -> tuple[bool, str | None]:
    """Shared validator for chain-event documents."""
    for field in required:
        if field not in body:
            return False, f"missing required field: {field}"
    if not isinstance(body.get("chain_id"), str) or not body["chain_id"]:
        return False, "chain_id must be non-empty string"
    if not isinstance(body.get("tx_hash"), str) or not body["tx_hash"]:
        return False, "tx_hash must be non-empty string"
    amt = body.get("amount")
    if not isinstance(amt, (int, float)) or not _is_finite(amt) or amt <= 0:
        return False, f"amount must be positive finite, got {amt!r}"
    return True, None


def validate_payment_received(body: dict) -> tuple[bool, str | None]:
    ok, err = _validate_chain_tx(body, [
        "chain_id", "tx_hash", "tx_index", "from_address", "to_address",
        "amount", "denom", "decimals", "confirmation",
    ])
    if not ok:
        return ok, err
    if not isinstance(body.get("confirmation"), dict):
        return False, "confirmation must be dict"
    if not isinstance(body.get("decimals"), int) or isinstance(body.get("decimals"), bool):
        return False, f"decimals must be int, got {type(body.get('decimals')).__name__}"
    return True, None


def validate_payment_finalized(body: dict) -> tuple[bool, str | None]:
    ok, err = _validate_chain_tx(body, [
        "chain_id", "tx_hash", "amount", "denom",
        "original_receipt_id", "finality",
    ])
    if not ok:
        return ok, err
    fin = body.get("finality", {})
    if not isinstance(fin, dict) or fin.get("level") != "finalized":
        return False, "finality.level must be 'finalized'"
    return True, None


def validate_payment_executed(body: dict) -> tuple[bool, str | None]:
    ok, err = _validate_chain_tx(body, [
        "chain_id", "tx_hash", "from_address", "to_address",
        "amount", "denom", "decimals", "settlement_ref", "finality",
    ])
    if not ok:
        return ok, err
    ref = body.get("settlement_ref", {})
    if not isinstance(ref, dict):
        return False, "settlement_ref must be dict"
    if not isinstance(body.get("finality"), dict):
        return False, f"finality must be dict, got {type(body.get('finality')).__name__}"
    if not isinstance(body.get("decimals"), int) or isinstance(body.get("decimals"), bool):
        return False, f"decimals must be int, got {type(body.get('decimals')).__name__}"
    return True, None


def validate_wallet_transfer(body: dict) -> tuple[bool, str | None]:
    ok, err = _validate_chain_tx(body, [
        "chain_id", "tx_hash", "from_address", "to_address",
        "amount", "denom", "decimals", "transfer_type",
    ])
    if not ok:
        return ok, err
    valid_types = {"hot_to_cold", "cold_to_hot", "derived_to_master",
                   "master_to_derived", "rebalance"}
    if body.get("transfer_type") not in valid_types:
        return False, f"invalid transfer_type: {body.get('transfer_type')}"
    return True, None


def validate_wallet_withdrawal(body: dict) -> tuple[bool, str | None]:
    ok, err = _validate_chain_tx(body, [
        "chain_id", "tx_hash", "from_address", "to_address",
        "amount", "denom", "decimals",
    ])
    if not ok:
        return ok, err
    if not isinstance(body.get("decimals"), int) or isinstance(body.get("decimals"), bool):
        return False, f"decimals must be int, got {type(body.get('decimals')).__name__}"
    return True, None


# ── v0.37.0: Admin Document Validators ───────────────────────────────


def validate_configuration_order(body: dict) -> tuple[bool, str | None]:
    for field in ["target", "operation", "changes"]:
        if field not in body:
            return False, f"missing required field: {field}"
    valid_ops = {"upsert_object", "modify_access", "remove_object"}
    if body.get("operation") not in valid_ops:
        return False, f"invalid operation: {body.get('operation')}"
    if not isinstance(body.get("changes"), dict):
        return False, "changes must be dict"
    return True, None


# ── v0.37.0: Disclosure Document Validators ───────────────────────────


def validate_punchhole_card(body: dict) -> tuple[bool, str | None]:
    for field in ["for_node", "for_access_level", "available", "not_available"]:
        if field not in body:
            return False, f"missing required field: {field}"
    if not isinstance(body.get("available"), list):
        return False, "available must be list"
    if not isinstance(body.get("not_available"), list):
        return False, "not_available must be list"
    return True, None


def validate_cache_object(body: dict) -> tuple[bool, str | None]:
    for field in ["object_key", "data", "granularity"]:
        if field not in body:
            return False, f"missing required field: {field}"
    if not isinstance(body.get("data"), dict):
        return False, "data must be dict"
    if not isinstance(body.get("granularity"), dict):
        return False, "granularity must be dict"
    return True, None


# ── v0.38.0: Netting Document Validators (A5.2) ───────────────────────


def validate_netting_reconcile(body: dict) -> tuple[bool, str | None]:
    """Validate a netting_reconcile document body."""
    for field in ["netting_id", "identity", "counterparty",
                  "proposed_net", "receipt_count", "chain_id"]:
        if field not in body:
            return False, f"missing required field: {field}"
    if not isinstance(body.get("netting_id"), str) or not body["netting_id"]:
        return False, "netting_id must be non-empty string"
    if not isinstance(body.get("chain_id"), str) or not body["chain_id"]:
        return False, "chain_id must be non-empty string"
    if not _is_finite(body.get("proposed_net", float("nan"))):
        return False, f"proposed_net must be finite number, got {body.get('proposed_net')!r}"
    if not isinstance(body.get("receipt_count"), int) or isinstance(body.get("receipt_count"), bool):
        return False, f"receipt_count must be int, got {type(body.get('receipt_count')).__name__}"
    if body["receipt_count"] < 0:
        return False, f"receipt_count must be >= 0, got {body['receipt_count']}"
    return True, None


def validate_netting_proposal(body: dict) -> tuple[bool, str | None]:
    """Validate a netting_proposal document body."""
    for field in ["netting_id", "identity", "counterparty",
                  "settlement_amount", "chain_id", "token_mint",
                  "target_address", "deadline"]:
        if field not in body:
            return False, f"missing required field: {field}"
    if not isinstance(body.get("netting_id"), str) or not body["netting_id"]:
        return False, "netting_id must be non-empty string"
    if not isinstance(body.get("chain_id"), str) or not body["chain_id"]:
        return False, "chain_id must be non-empty string"
    if not isinstance(body.get("target_address"), str) or not body["target_address"]:
        return False, "target_address must be non-empty string"
    if not isinstance(body.get("deadline"), str) or not body["deadline"]:
        return False, "deadline must be non-empty string"
    amt = body.get("settlement_amount")
    if not _is_finite(amt) or amt <= 0:
        return False, f"settlement_amount must be positive finite, got {amt!r}"
    return True, None


def validate_netting_acceptance(body: dict) -> tuple[bool, str | None]:
    """Validate a netting_acceptance document body."""
    for field in ["netting_id", "proposal_ref", "identity", "counterparty",
                  "accepted_amount", "source_address"]:
        if field not in body:
            return False, f"missing required field: {field}"
    if not isinstance(body.get("netting_id"), str) or not body["netting_id"]:
        return False, "netting_id must be non-empty string"
    if not isinstance(body.get("proposal_ref"), str) or not body["proposal_ref"]:
        return False, "proposal_ref must be non-empty string"
    if not isinstance(body.get("source_address"), str) or not body["source_address"]:
        return False, "source_address must be non-empty string"
    amt = body.get("accepted_amount")
    if not _is_finite(amt) or amt <= 0:
        return False, f"accepted_amount must be positive finite, got {amt!r}"
    return True, None


def validate_netting_executed(body: dict) -> tuple[bool, str | None]:
    """Validate a netting_executed document body."""
    for field in ["netting_id", "acceptance_ref", "identity", "counterparty",
                  "tx_hash", "chain_id", "amount"]:
        if field not in body:
            return False, f"missing required field: {field}"
    if not isinstance(body.get("netting_id"), str) or not body["netting_id"]:
        return False, "netting_id must be non-empty string"
    if not isinstance(body.get("tx_hash"), str) or not body["tx_hash"]:
        return False, "tx_hash must be non-empty string"
    if not isinstance(body.get("chain_id"), str) or not body["chain_id"]:
        return False, "chain_id must be non-empty string"
    amt = body.get("amount")
    if not _is_finite(amt) or amt <= 0:
        return False, f"amount must be positive finite, got {amt!r}"
    return True, None
