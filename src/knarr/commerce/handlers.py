"""Commerce document handlers — async closures for SyncEngine dispatch.

Each handler is an async callable taking a single ``item: dict`` argument,
matching the signature expected by ``SyncEngine._dispatch_system_item``:

    await handler(item)

Node access is captured via closure from ``make_commerce_handlers(node)``.
"""
import hashlib
import logging
import time

from .schemas import (
    validate_receipt,
    validate_credit_note,
    validate_settle_request,
    validate_settlement_confirmation,
)

logger = logging.getLogger("knarr.commerce")


def make_commerce_handlers(node) -> dict:
    """Factory returning {msg_type: async handler} dict for commerce mail.

    Usage in node.py::

        from ..commerce.handlers import make_commerce_handlers
        for msg_type, handler in make_commerce_handlers(self).items():
            self._sync.register_handler(msg_type, handler)
    """

    async def handle_receipt(item: dict) -> None:
        """Process knarr/commerce/receipt mail."""
        body = _parse_body(item)
        if body is None:
            return

        valid, err = validate_receipt(body)
        if not valid:
            logger.warning(f"Invalid receipt from {(item.get('from_node') or '?')[:16]}: {err}")
            return

        task_id = body["task_id"]
        quality_rating = body.get("quality_rating")

        # Store quality_rating in execution_log
        if quality_rating is not None:
            await node._enqueue_write(node.storage.update_receipt_quality, task_id, quality_rating)

        # Rejected with refund_requested → auto-generate credit_note
        # S-022: Verify sender is the legitimate consumer (requester)
        if body["status"] == "rejected" and body.get("refund_requested"):
            original = node.storage.get_execution_log_entry(task_id)
            if original and original.get("price"):
                # S-022: Verify the refund request comes from the original requester
                from_node = item.get("from_node")
                expected_requester = original.get("requester_node_id")
                # F5: Reject when either side is None/empty (NULL caller_node_id or missing from_node)
                if not from_node or not expected_requester or from_node != expected_requester:
                    logger.warning(f"REFUND_SENDER_MISMATCH task={task_id[:8]}: "
                                   f"from={from_node[:16] if from_node else 'N/A'} "
                                   f"expected={expected_requester[:16] if expected_requester else 'N/A'}")
                    return

                credit_note = {
                    "type": "knarr/commerce/credit_note",
                    "references": {"task_id": task_id, "original_amount": original["price"]},
                    "amount": original["price"],
                    "reason": "quality_rejection",
                    "initiated_by": "provider",
                    "timestamp": time.time(),
                    "schema_version": "1.0",
                }
                await node._sync.enqueue(
                    to_node=item.get("from_node"),
                    msg_type="knarr/commerce/credit_note",
                    body=credit_note,
                    system=True,
                )

    async def handle_credit_note(item: dict) -> None:
        """Process knarr/commerce/credit_note mail."""
        body = _parse_body(item)
        if body is None:
            return

        valid, err = validate_credit_note(body)
        if not valid:
            logger.warning(f"Invalid credit_note from {(item.get('from_node') or '?')[:16]}: {err}")
            return

        amount = body["amount"]

        # Inflation guard: require references.task_id and validate against local records
        refs = body.get("references", {})
        task_id = refs.get("task_id")
        if not task_id:
            logger.warning(f"Credit note rejected: missing references.task_id — no unverifiable credits")
            return

        # Look up the original execution to cap the refund — MUST have local record
        original = node.storage.get_execution_log_entry(task_id)
        if not original or not original.get("price"):
            logger.warning(f"Credit note rejected: no local execution record for task_id={task_id[:16]}")
            return

        # S-021: Single refund sanity check (amount > 0, not exceeding 2x in itself)
        max_refund = original["price"] * 2  # 2x cap as safety margin
        if amount <= 0 or amount > max_refund:
            logger.warning(f"Credit note rejected: amount {amount} invalid for price {original['price']}")
            return

        # F11: Verify credit_note sender matches the original task consumer
        from_node_id = item.get("from_node")
        expected_requester = original.get("requester_node_id")
        if not from_node_id or not expected_requester:
            logger.warning(f"CREDIT_NOTE_SENDER_UNVERIFIED task={task_id[:8]}: "
                           f"from={from_node_id!r} expected={expected_requester!r}")
            return
        if from_node_id != expected_requester:
            logger.warning(f"CREDIT_NOTE_SENDER_MISMATCH task={task_id[:8]}: "
                           f"from={from_node_id[:16]} expected={expected_requester[:16]}")
            return

        target_pubkey = _resolve_public_key(node, from_node_id)

        if target_pubkey:
            # S-021: Atomic record_refund — cap check + increment in single SQL
            recorded = await node._enqueue_write(node.storage.record_refund, task_id, amount)
            if not recorded:
                cumulative = node.storage.get_cumulative_refund(task_id)
                logger.warning(f"Credit note rejected: cumulative refund would exceed "
                               f"2x original {original['price']:.2f} for task_id={task_id[:16]} "
                               f"(current={cumulative:.2f})")
                return
            await node._enqueue_write(node.storage.update_ledger_refund, target_pubkey, amount)
            # F15: Refund restores credit — check if peer crosses back below threshold
            _balance_after = node.storage.get_ledger_balance(target_pubkey)
            if _balance_after is not None and hasattr(node, '_check_credit_restored'):
                # old = before refund (lower), new = after refund (higher)
                node._check_credit_restored(target_pubkey, _balance_after - amount, _balance_after)
            cumulative = node.storage.get_cumulative_refund(task_id)
            logger.info(f"CREDIT_NOTE task={body.get('references', {}).get('task_id', 'N/A')[:8]} "
                        f"amount={amount} reason={body.get('reason')} cumulative={cumulative:.2f}/{max_refund:.2f}")
        else:
            logger.warning(f"Could not resolve public_key for node_id {(from_node_id or '?')[:16]} — credit note dropped")

    async def handle_settle_request(item: dict) -> None:
        """Process knarr/commerce/settle_request mail."""
        body = _parse_body(item)
        if body is None:
            return

        valid, err = validate_settle_request(body)
        if not valid:
            logger.warning(f"Invalid settle_request: {err}")
            return

        await node._enqueue_write(
            node.storage.queue_settlement,
            "settle_request",
            item.get("from_node"),
            body,
            1,  # priority
        )
        logger.info(f"SETTLE_REQUEST from={item.get('from_node', '?')[:16]} "
                    f"balance={body['current_balance']} limit={body['credit_limit']}")

    async def handle_settlement_confirmation(item: dict) -> None:
        """Process knarr/commerce/settlement_confirmation mail."""
        body = _parse_body(item)
        if body is None:
            return

        valid, err = validate_settlement_confirmation(body)
        if not valid:
            logger.warning(f"Invalid settlement_confirmation: {err}")
            return

        await node._enqueue_write(
            node.storage.queue_settlement,
            "settlement_confirmation",
            item.get("from_node"),
            body,
            0,  # priority
        )
        logger.info(f"SETTLEMENT_CONFIRM from={item.get('from_node', '?')[:16]} "
                    f"tx={body['tx_hash'][:16]} amount={body['amount_settled']}")

    return {
        "knarr/commerce/receipt": handle_receipt,
        "knarr/commerce/credit_note": handle_credit_note,
        "knarr/commerce/settle_request": handle_settle_request,
        "knarr/commerce/settlement_confirmation": handle_settlement_confirmation,
    }


# ---------- helpers ----------

def _parse_body(item: dict) -> dict | None:
    """Extract body from mail item, handling string-encoded JSON."""
    body = item.get("body", {})
    if isinstance(body, str):
        import json
        try:
            body = json.loads(body)
        except Exception:
            return None
    return body


def _resolve_public_key(node, node_id: str) -> str | None:
    """Reverse-lookup: node_id → peer_public_key via ledger entries.

    Uses SHA-256(public_key) == node_id to find the match.
    Falls back to O(n) scan of ledger entries since there's no reverse index.
    """
    if not node_id:
        return None
    entries = node.storage.get_all_ledger_entries()
    for entry in entries:
        pk = entry["peer_public_key"]
        try:
            computed_nid = hashlib.sha256(bytes.fromhex(pk)).hexdigest()
            if computed_nid == node_id:
                return pk
        except Exception:
            continue
    return None
