"""
Settlement execution — two-signature flow (B2).

The node prepares, the authority countersigns, the node executes.
Every step produces a signed receipt with dual signatures.

Flow:
    1. prepare_settlement()  → settlement_prepared Document (signed #key-1)
    2. on_settlement_review() → countersigned Document (#thrall-1 or #cockpit-1)
    3. validate_dual_signatures() → both proofs verified
    4. execute_settlement()  → write receipt, send mail, emit bus event

Bus events carry full Document + Proof in payload_json (WM-ready).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Optional, Tuple

from ..core.crypto import SigningKey, VerifyKey

from .conversion import credits_to_token, get_conversion_rate
from .documents import Document, settlement_prepared, settlement_accepted, settlement_processed
from .receipt_writer import write_receipt
from ..core.proof import sign_document, verify_document

logger = logging.getLogger(__name__)

# E-01: Module-level debug flag — set via configure_debug() from node init.
# Previously, handler code referenced an instance debug attribute without
# initialization, causing AttributeError on every debug log line.
_debug = False


def configure_debug(node) -> None:
    """Initialize module-level debug flag from node config. Call once at startup."""
    global _debug
    _debug = getattr(node, '_debug', False)


async def prepare_settlement(
    node_id: str,
    peer_key: str,
    amount: float,
    formula: str,
    proposer_balance: float,
    counterparty_balance_claimed: float,
    utilization: float,
    target_utilization: float,
    signing_key: SigningKey,
    storage,
    bus=None,
) -> dict:
    """Build and sign settlement_prepared Document.

    Returns the signed document dict ready for authority review.
    Writes a receipt to the receipt_log for audit trail.

    Args:
        node_id:                     Proposer's node_id (hex).
        peer_key:                    Counterparty public key (hex).
        amount:                      Settlement amount from engine.
        formula:                     Human-readable formula used.
        proposer_balance:            Our ledger balance for this peer.
        counterparty_balance_claimed: Our claim of counterparty's balance (mirror).
        utilization:                 Current utilization that triggered settlement.
        target_utilization:          Target utilization post-settlement.
        signing_key:                 Node's Ed25519 signing key (#key-1).
        storage:                     Storage instance for receipt writing.
        bus:                         EventBus for emitting events (optional).

    Returns:
        Signed document dict (payload + proof).
    """
    verification_method = f"did:knarr:{node_id}#key-1"

    doc = settlement_prepared(
        proposer=node_id,
        counterparty=peer_key,
        amount=round(amount, 6),
        formula=formula,
        proposer_balance=round(proposer_balance, 6),
        counterparty_balance_claimed=round(counterparty_balance_claimed, 6),
        utilization=round(utilization, 6),
        target_utilization=round(target_utilization, 6),
    )

    payload = doc.payload
    signed_doc = sign_document(payload, signing_key, verification_method)

    # Write receipt for audit trail
    receipt_id = doc["receipt_id"]
    payload_json_str = json.dumps(signed_doc, sort_keys=True, separators=(",", ":"))
    signature = signed_doc["proof"]["proofValue"]

    try:
        storage.write_receipt(
            receipt_id=receipt_id,
            document_type="settlement_prepared",
            timestamp=doc["timestamp"],
            identity=node_id,
            counterparty=peer_key,
            order_ref=None,
            proof_purpose="assertionMethod",
            payload_json=payload_json_str,
            signature=signature,
        )
    except Exception as exc:
        logger.warning(f"SETTLEMENT_PREPARED_RECEIPT_FAIL id={receipt_id}: {exc}")

    if bus:
        bus.emit(
            "settlement.prepared",
            receipt_id=receipt_id,
            document_type="settlement_prepared",
            identity=verification_method,
            counterparty=peer_key,
            payload_json=payload_json_str,
        )

    if _debug:
        logger.info(
            f"SETTLEMENT_PREPARED_DEBUG id={receipt_id[:16]} "
            f"peer={peer_key[:16]} amount={amount:.2f} "
            f"proposer_balance={proposer_balance:.4f} utilization={utilization:.4f}"
        )

    logger.info(
        f"SETTLEMENT_PREPARED id={receipt_id[:16]} "
        f"peer={peer_key[:16]} amount={amount:.2f}"
    )

    return signed_doc


def validate_dual_signatures(
    prepared_doc: dict,
    countersigned_doc: dict,
    node_verify_key: VerifyKey,
    authority_verify_key: VerifyKey,
) -> Tuple[bool, str]:
    """Validate that a settlement document carries two valid signatures.

    The prepared_doc must verify with the node key (#key-1).
    The countersigned_doc must carry a second proof verifiable with
    the authority key (#thrall-1 or #cockpit-1).

    Both proofs must cover the same document payload.

    Returns:
        (True, "") on success, (False, reason) on any failure.
    """
    # 1. Verify the node's signature on prepared_doc
    if not isinstance(prepared_doc, dict) or "proof" not in prepared_doc:
        return False, "prepared_doc missing proof"

    if not verify_document(prepared_doc, node_verify_key):
        return False, "Node signature (#key-1) verification failed"

    # 2. Verify the authority's signature on countersigned_doc
    if not isinstance(countersigned_doc, dict) or "proof" not in countersigned_doc:
        return False, "countersigned_doc missing proof"

    if not verify_document(countersigned_doc, authority_verify_key):
        return False, "Authority signature verification failed"

    # 3. Ensure the document payloads are identical (strip proofs for comparison)
    def _strip_proof(d: dict) -> dict:
        return {k: v for k, v in d.items() if k != "proof"}

    prepared_payload = _strip_proof(prepared_doc)
    countersigned_payload = _strip_proof(countersigned_doc)

    if prepared_payload != countersigned_payload:
        # Log first diverging key for diagnostics
        for key in set(list(prepared_payload.keys()) + list(countersigned_payload.keys())):
            if prepared_payload.get(key) != countersigned_payload.get(key):
                logger.warning(
                    f"DUAL_SIG_PAYLOAD_MISMATCH key={key} "
                    f"prepared={str(prepared_payload.get(key))[:32]} "
                    f"countersigned={str(countersigned_payload.get(key))[:32]}"
                )
                break
        return False, "Document payloads differ between prepared and countersigned"

    return True, ""


async def execute_settlement(
    prepared_doc: dict,
    countersigned_doc: dict,
    node_verify_key: VerifyKey,
    authority_verify_key: VerifyKey,
    node_id: str,
    signing_key: SigningKey,
    peer_key: str,
    storage,
    send_mail_fn: Callable,
    bus=None,
    config: Optional[dict] = None,
    provider_wallet: str = "",
    enqueue_write: Optional[Callable] = None,
) -> str:
    """Validate dual signatures, write accepted receipt, send settle_request mail.

    This is the commit point: after this function, the settlement has been
    sent to the counterparty. It does NOT zero the ledger — that happens
    when the counterparty sends settlement_confirmation back.

    Args:
        prepared_doc:       Signed document (node signature).
        countersigned_doc:  Dual-signed document (node + authority).
        node_verify_key:    Node's Ed25519 verify key.
        authority_verify_key: Authority's Ed25519 verify key.
        node_id:            Our node_id.
        signing_key:        Node signing key (for settlement_accepted receipt).
        peer_key:           Counterparty public key (hex).
        storage:            Storage instance.
        send_mail_fn:       Async callable: (to_node, msg_type, body) -> None.
        bus:                EventBus for emitting events.

    Returns:
        The receipt_id of the settlement_accepted receipt.

    Raises:
        ValueError: if dual signature validation fails.
    """
    # Validate both signatures
    ok, reason = validate_dual_signatures(
        prepared_doc, countersigned_doc, node_verify_key, authority_verify_key
    )
    if not ok:
        logger.error(
            f"SETTLEMENT_EXEC_REJECTED peer={peer_key[:16]} reason={reason}"
        )
        raise ValueError(f"Dual signature validation failed: {reason}")

    # Extract fields from the document for the accepted receipt
    doc_body = {k: v for k, v in countersigned_doc.items() if k != "proof"}
    amount = float(doc_body.get("amount", 0.0) or 0.0)
    if amount <= 0:
        raise ValueError(f"Settlement amount must be positive, got {amount}")
    token_amount = credits_to_token(amount, get_conversion_rate(config or {}))
    prepared_receipt_id = doc_body.get("receipt_id", "")

    # Determine authority info from countersigned proof
    auth_proof = countersigned_doc.get("proof", {})
    authority_method = auth_proof.get("verificationMethod", "unknown")

    verification_method = f"did:knarr:{node_id}#key-1"

    # Build settlement_accepted Document
    doc = settlement_accepted(
        proposer=node_id,
        counterparty=peer_key,
        amount=amount,
        authority=authority_method.split("#")[-1] if "#" in authority_method else authority_method,
        authority_method=authority_method,
        prepared_receipt_id=prepared_receipt_id,
    )

    payload = doc.payload
    signed_accepted = sign_document(payload, signing_key, verification_method)

    receipt_id = doc["receipt_id"]
    payload_json_str = json.dumps(signed_accepted, sort_keys=True, separators=(",", ":"))
    signature = signed_accepted["proof"]["proofValue"]

    # Write settlement_accepted receipt
    loop = asyncio.get_running_loop()

    async def _storage_write(op, *args):
        if enqueue_write is not None:
            return await enqueue_write(op, *args)
        return await loop.run_in_executor(None, lambda: op(*args))

    async def _storage_read(op, *args):
        return await loop.run_in_executor(None, lambda: op(*args))

    # Dedup guard: skip if this receipt was already written (concurrent submission)
    existing = await _storage_read(storage.get_receipt, receipt_id)
    if existing is not None:
        logger.warning(f"SETTLEMENT_DUPLICATE_RECEIPT id={receipt_id[:16]} — skipping")
        return receipt_id

    def _write_accepted_receipt() -> None:
        storage.write_receipt(
            receipt_id=receipt_id,
            document_type="settlement_accepted",
            timestamp=doc["timestamp"],
            identity=node_id,
            counterparty=peer_key,
            order_ref=prepared_receipt_id or None,
            proof_purpose="assertionMethod",
            payload_json=payload_json_str,
            signature=signature,
        )

    try:
        await _storage_write(_write_accepted_receipt)
    except Exception as exc:
        logger.warning(f"SETTLEMENT_ACCEPTED_RECEIPT_FAIL id={receipt_id}: {exc}")

    # Emit WM-ready bus event
    if bus:
        bus.emit(
            "settlement.accepted",
            receipt_id=receipt_id,
            document_type="settlement_accepted",
            identity=verification_method,
            counterparty=peer_key,
            payload_json=payload_json_str,
        )

    # Send settle_request mail to counterparty
    # Resolve peer_key (public key hex) → node_id for mail routing
    to_node = await _storage_read(storage.get_node_id_by_pubkey, peer_key)
    if not to_node:
        raise ValueError(
            f"execute_settlement: cannot resolve peer_key to node_id — "
            f"peer_key={peer_key[:16]} not in peer_keys table"
        )

    # Resolve balance and credit_limit for settle_request body
    current_balance = await _storage_read(storage.get_ledger_balance, peer_key) or 0.0
    ledger_entry = await _storage_write(storage.get_or_create_ledger_entry, peer_key)
    credit_limit = ledger_entry.hard_limit

    settle_body = {
        "type": "knarr/commerce/settle_request",
        "document": countersigned_doc,
        "prepared_proof": prepared_doc.get("proof"),
        "authority_proof": countersigned_doc.get("proof"),
        "peer_key": peer_key,
        "amount": token_amount,
        "accepted_receipt_id": receipt_id,
        "current_balance": current_balance,
        "credit_limit": credit_limit,
        "provider_wallet": provider_wallet,
        "schema_version": "1.0",
        "timestamp": time.time(),
    }

    try:
        await send_mail_fn(
            to_node=to_node,
            msg_type="knarr/commerce/settle_request",
            body=settle_body,
            system=True,
        )
    except Exception as mail_err:
        logger.warning(
            f"SETTLEMENT_MAIL_FAIL peer={peer_key[:16]} "
            f"receipt={receipt_id[:16]}: {mail_err}"
        )

    if _debug:
        logger.info(
            f"SETTLEMENT_EXECUTED_DEBUG receipt={receipt_id[:16]} "
            f"peer={peer_key[:16]} token_amount={token_amount:.2f} "
            f"current_balance={current_balance:.4f} credit_limit={credit_limit}"
        )

    logger.info(
        f"SETTLEMENT_EXECUTED receipt={receipt_id[:16]} "
        f"peer={peer_key[:16]} amount={token_amount:.2f}"
    )

    return receipt_id


async def write_settlement_processed(
    node_id: str,
    peer_key: str,
    amount_settled: float,
    ledger_delta: float,
    final_balance: float,
    accepted_receipt_id: str,
    settle_request_ref: str,
    signing_key: SigningKey,
    storage,
    bus=None,
) -> str:
    """Write a settlement_processed receipt after zeroing the bilateral ledger.

    Called by the proposer once the counterparty confirms the settlement.

    Returns:
        receipt_id of the settlement_processed document.
    """
    verification_method = f"did:knarr:{node_id}#key-1"

    doc = settlement_processed(
        proposer=node_id,
        counterparty=peer_key,
        amount_settled=round(amount_settled, 6),
        ledger_delta=round(ledger_delta, 6),
        final_balance=round(final_balance, 6),
        accepted_receipt_id=accepted_receipt_id,
        settle_request_ref=settle_request_ref,
    )

    payload = doc.payload
    signed_doc = sign_document(payload, signing_key, verification_method)

    receipt_id = doc["receipt_id"]
    payload_json_str = json.dumps(signed_doc, sort_keys=True, separators=(",", ":"))
    signature = signed_doc["proof"]["proofValue"]

    try:
        storage.write_receipt(
            receipt_id=receipt_id,
            document_type="settlement_processed",
            timestamp=doc["timestamp"],
            identity=node_id,
            counterparty=peer_key,
            order_ref=accepted_receipt_id or None,
            proof_purpose="assertionMethod",
            payload_json=payload_json_str,
            signature=signature,
        )
    except Exception as exc:
        logger.warning(f"SETTLEMENT_PROCESSED_RECEIPT_FAIL id={receipt_id}: {exc}")

    if bus:
        bus.emit(
            "settlement.processed",
            receipt_id=receipt_id,
            document_type="settlement_processed",
            identity=verification_method,
            counterparty=peer_key,
            payload_json=payload_json_str,
        )

    logger.info(
        f"SETTLEMENT_PROCESSED receipt={receipt_id[:16]} "
        f"peer={peer_key[:16]} amount={amount_settled:.2f} "
        f"final_balance={final_balance:.2f}"
    )

    return receipt_id
