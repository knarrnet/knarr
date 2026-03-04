"""
Document construction layer (Layer 1).

Validates shapes, not signatures. CloudEvents pattern:
constructor-time validation, auto-populated defaults, mapping protocol.

Layer 1 of the two-layer receipt architecture:
- Knows about document SHAPES, not about signing.
- Type registry with per-type required fields (Python sets).
- Convenience factories for common types.
"""

from __future__ import annotations

import copy
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Set

from ..core import rfc8785

# ── Type Registry ──────────────────────────────────────────────────────
# document_type -> required fields (Python sets, CloudEvents precedent).
# Extensions (extra fields) are implicit — accepted without validation.

_TYPE_REGISTRY: Dict[str, Set[str]] = {
    "execution_receipt": {"skill_name", "provider", "consumer", "status"},
    "credit_note": {"provider", "consumer", "amount", "skill_name"},
    "order_ack": {"skill_name", "status"},
    "order_executing": {"skill_name", "task_id"},
    "mail_delivery_receipt": {"recipient", "message_id", "status"},
    "mail_receive_receipt": {"sender", "message_id"},
    "admission_decision": {"skill_name", "decision"},
    "price_calculation": {"skill_name", "inputs", "calculation", "output"},
    # v0.36.0: Settlement document types
    "settlement_prepared": {
        "proposer", "counterparty", "amount", "formula",
        "proposer_balance", "counterparty_balance_claimed",
        "utilization", "target_utilization",
    },
    "settlement_accepted": {
        "proposer", "counterparty", "amount",
        "authority", "authority_method",
        "prepared_receipt_id",
    },
    "settlement_processed": {
        "proposer", "counterparty", "amount_settled",
        "ledger_delta", "final_balance",
        "accepted_receipt_id", "settle_request_ref",
    },
    "settlement_confirmation": {
        "proposer", "counterparty", "amount_confirmed",
        "own_final_balance", "processed_receipt_id",
    },
}

# ── Prefix Map ─────────────────────────────────────────────────────────
# document_type -> receipt_id prefix

_PREFIX_MAP: Dict[str, str] = {
    "execution_receipt": "exec",
    "credit_note": "cn",
    "order_ack": "oack",
    "order_executing": "oexe",
    "mail_delivery_receipt": "mdr",
    "mail_receive_receipt": "mrr",
    "admission_decision": "adm",
    "price_calculation": "prc",
    # v0.36.0: Settlement document type prefixes
    "settlement_prepared": "sp",
    "settlement_accepted": "sa",
    "settlement_processed": "spr",
    "settlement_confirmation": "sc",
}


class Document:
    """A validated, timestamped document ready for signing.

    Construction validates required fields and auto-populates defaults.
    Implements mapping protocol for read access (doc["field"]).

    Extra fields beyond the required set are accepted (CloudEvents pattern:
    extensions are implicit). Pass them via the fields dict.
    """

    __slots__ = ("_type", "_payload")

    def __init__(self, document_type: str, fields: Dict[str, Any]) -> None:
        if document_type not in _TYPE_REGISTRY:
            raise ValueError(f"Unknown document type: {document_type}")

        required = _TYPE_REGISTRY[document_type]
        missing = required - set(fields.keys())
        if missing:
            raise ValueError(
                f"Missing required fields for {document_type}: {sorted(missing)}"
            )

        self._type = document_type
        prefix = _PREFIX_MAP.get(document_type, "doc")

        # Auto-populated defaults (deep copy to isolate from caller mutations)
        self._payload: Dict[str, Any] = copy.deepcopy(fields)
        self._payload["document_type"] = document_type
        self._payload["version"] = 2
        self._payload["receipt_id"] = f"{prefix}_{secrets.token_hex(8)}"
        self._payload["timestamp"] = _iso_now()

    @property
    def document_type(self) -> str:
        return self._type

    @property
    def payload(self) -> dict:
        """Returns a deep copy of the payload dict (for signing layer)."""
        return copy.deepcopy(self._payload)

    def canonical_json(self) -> str:
        """JCS-canonical JSON string (RFC 8785)."""
        return rfc8785.dumps(self._payload).decode("utf-8")

    # Mapping protocol (read-only)

    def __getitem__(self, key: str) -> Any:
        return self._payload[key]

    def __contains__(self, key: str) -> bool:
        return key in self._payload

    def get(self, key: str, default: Any = None) -> Any:
        return self._payload.get(key, default)


def _iso_now() -> str:
    """UTC ISO 8601 timestamp with millisecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ── Convenience Factories ──────────────────────────────────────────────
# All accept **extra for extension fields (CloudEvents pattern).


def execution_receipt(
    provider: str, consumer: str, skill_name: str, status: str, **extra: Any
) -> Document:
    return Document("execution_receipt", {
        "provider": provider, "consumer": consumer,
        "skill_name": skill_name, "status": status, **extra,
    })


def credit_note(
    provider: str, consumer: str, skill_name: str, amount: float, **extra: Any
) -> Document:
    return Document("credit_note", {
        "provider": provider, "consumer": consumer,
        "skill_name": skill_name, "amount": amount, **extra,
    })


def order_ack(skill_name: str, status: str, **extra: Any) -> Document:
    return Document("order_ack", {
        "skill_name": skill_name, "status": status, **extra,
    })


def order_executing(skill_name: str, task_id: str, **extra: Any) -> Document:
    return Document("order_executing", {
        "skill_name": skill_name, "task_id": task_id, **extra,
    })


def mail_delivery_receipt(
    recipient: str, message_id: str, status: str, **extra: Any
) -> Document:
    return Document("mail_delivery_receipt", {
        "recipient": recipient, "message_id": message_id,
        "status": status, **extra,
    })


def mail_receive_receipt(sender: str, message_id: str, **extra: Any) -> Document:
    return Document("mail_receive_receipt", {
        "sender": sender, "message_id": message_id, **extra,
    })


def admission_decision(
    skill_name: str, decision: dict, **extra: Any
) -> Document:
    return Document("admission_decision", {
        "skill_name": skill_name, "decision": decision, **extra,
    })


def price_calculation(
    skill_name: str, inputs: dict, calculation: dict, output: dict, **extra: Any
) -> Document:
    return Document("price_calculation", {
        "skill_name": skill_name, "inputs": inputs,
        "calculation": calculation, "output": output, **extra,
    })


# ── v0.36.0: Settlement Document Factories ──────────────────────────────


def settlement_prepared(
    proposer: str, counterparty: str, amount: float,
    formula: str, proposer_balance: float,
    counterparty_balance_claimed: float,
    utilization: float, target_utilization: float,
    **extra: Any,
) -> Document:
    """Factory for settlement_prepared document.

    Created when node prepares a settlement for authority review.
    """
    return Document("settlement_prepared", {
        "proposer": proposer,
        "counterparty": counterparty,
        "amount": amount,
        "formula": formula,
        "proposer_balance": proposer_balance,
        "counterparty_balance_claimed": counterparty_balance_claimed,
        "utilization": utilization,
        "target_utilization": target_utilization,
        **extra,
    })


def settlement_accepted(
    proposer: str, counterparty: str, amount: float,
    authority: str, authority_method: str,
    prepared_receipt_id: str,
    **extra: Any,
) -> Document:
    """Factory for settlement_accepted document.

    Created when authority countersigns the prepared settlement.
    """
    return Document("settlement_accepted", {
        "proposer": proposer,
        "counterparty": counterparty,
        "amount": amount,
        "authority": authority,
        "authority_method": authority_method,
        "prepared_receipt_id": prepared_receipt_id,
        **extra,
    })


def settlement_processed(
    proposer: str, counterparty: str, amount_settled: float,
    ledger_delta: float, final_balance: float,
    accepted_receipt_id: str, settle_request_ref: str,
    **extra: Any,
) -> Document:
    """Factory for settlement_processed document.

    Created when counterparty confirms and ledger is zeroed.
    """
    return Document("settlement_processed", {
        "proposer": proposer,
        "counterparty": counterparty,
        "amount_settled": amount_settled,
        "ledger_delta": ledger_delta,
        "final_balance": final_balance,
        "accepted_receipt_id": accepted_receipt_id,
        "settle_request_ref": settle_request_ref,
        **extra,
    })


def settlement_confirmation(
    proposer: str, counterparty: str, amount_confirmed: float,
    own_final_balance: float, processed_receipt_id: str,
    **extra: Any,
) -> Document:
    """Factory for settlement_confirmation document.

    Created when counterparty accepts and confirms the settlement.
    """
    return Document("settlement_confirmation", {
        "proposer": proposer,
        "counterparty": counterparty,
        "amount_confirmed": amount_confirmed,
        "own_final_balance": own_final_balance,
        "processed_receipt_id": processed_receipt_id,
        **extra,
    })
