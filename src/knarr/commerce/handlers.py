"""Commerce document handlers — async closures for SyncEngine dispatch.

Each handler is an async callable taking a single ``item: dict`` argument,
matching the signature expected by ``SyncEngine._dispatch_system_item``:

    await handler(item)

Node access is captured via closure from ``make_commerce_handlers(node)``.
"""
import asyncio
import hashlib
import logging
import time

from .schemas import (
    validate_receipt,
    validate_credit_note,
    validate_settle_request,
    validate_settlement_confirmation,
    validate_tab_reminder,
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
            logger.warning(f"Invalid receipt from {item.get('from_node', '?')[:16]}: {err}")
            return

        task_id = body["task_id"]
        quality_rating = body.get("quality_rating")

        # Store quality_rating in execution_log
        if quality_rating is not None:
            await node._enqueue_write(node.storage.update_receipt_quality, task_id, quality_rating)

        # Rejected with refund_requested → auto-generate credit_note
        if body["status"] == "rejected" and body.get("refund_requested"):
            original = node.storage.get_execution_log_entry(task_id)
            if original and original.get("price"):
                token_symbol = getattr(node, "_token_symbol", "KNARR")
                credit_note = {
                    "type": "knarr/commerce/credit_note",
                    "references": {"task_id": task_id, "original_amount": original["price"]},
                    "amount": original["price"],
                    "token": token_symbol,
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
            logger.warning(f"Invalid credit_note from {item.get('from_node', '?')[:16]}: {err}")
            return

        amount = body["amount"]
        token = body.get("token", "KNARR")

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
        max_refund = original["price"]  # 1x cap — refund cannot exceed original price
        if amount > max_refund:
            logger.warning(f"Credit note rejected: amount {amount} > original {original['price']}")
            return

        from_node_id = item.get("from_node")

        # F-07: Verify that the sender is the original provider — not just any peer
        # who knows the task_id. provider_node_id is stored in the execution log.
        provider_node_id = original.get("provider_node_id")
        if provider_node_id and from_node_id != provider_node_id:
            logger.warning(
                f"Credit note rejected: sender {str(from_node_id)[:16]} "
                f"!= provider {str(provider_node_id)[:16]} for task_id={task_id[:16]}"
            )
            return

        # Dedup: reject if this task_id has already been refunded
        refund_receipt_id = f"credit_note_refund:{task_id}"
        if node.storage.get_receipt(refund_receipt_id) is not None:
            logger.warning(
                f"Credit note rejected: duplicate refund for task_id={task_id[:16]} — already applied"
            )
            return

        target_pubkey = _resolve_public_key(node, from_node_id)

        if target_pubkey:
            # Write dedup receipt before applying to prevent replay
            import time as _time
            node.storage.write_receipt(
                receipt_id=refund_receipt_id,
                document_type="credit_note_refund",
                timestamp=str(_time.time()),
                identity=node.node_info.node_id,
                counterparty=target_pubkey,
                order_ref=task_id,
                proof_purpose="refundApplied",
                payload_json=f'{{"task_id":"{task_id}","amount":{amount}}}',
                signature=None,
            )
            await node._enqueue_write(node.storage.update_ledger_refund, target_pubkey, amount)
            logger.info(f"CREDIT_NOTE task={body.get('references', {}).get('task_id', 'N/A')[:8]} "
                        f"amount={amount} token={token} reason={body.get('reason')}")
        else:
            logger.warning(f"Could not resolve public_key for node_id {from_node_id[:16]} — credit note dropped")

    async def handle_settle_request(item: dict) -> None:
        """Process knarr/commerce/settle_request mail."""
        body = _parse_body(item)
        if body is None:
            return

        valid, err = validate_settle_request(body)
        if not valid:
            logger.warning(f"Invalid settle_request: {err}")
            return

        # Dedup: reject if this accepted_receipt_id was already processed
        accepted_receipt_id = body.get("accepted_receipt_id")
        if accepted_receipt_id:
            existing = node.storage.get_receipt(accepted_receipt_id)
            if existing is not None:
                logger.warning(f"SETTLE_REQUEST replay detected: receipt_id={accepted_receipt_id[:16]} already seen")
                return

        await node._enqueue_write(
            node.storage.queue_settlement,
            "settle_request",
            item.get("from_node"),
            body,
            1,  # priority
        )
        logger.info(f"SETTLE_REQUEST from={item.get('from_node', '?')[:16]} "
                    f"balance={body['current_balance']} limit={body['credit_limit']} "
                    f"token={body.get('token', 'KNARR')}")

    async def handle_settlement_confirmation(item: dict) -> None:
        """Process knarr/commerce/settlement_confirmation mail."""
        body = _parse_body(item)
        if body is None:
            return

        valid, err = validate_settlement_confirmation(body)
        if not valid:
            logger.warning(f"Invalid settlement_confirmation: {err}")
            return

        from_node = item.get("from_node")
        tx_hash = body.get("tx_hash", "")

        # Verify this confirmation matches a settlement we initiated with this peer.
        # Look for a settlement_accepted receipt with order_ref matching tx_hash,
        # or at minimum that we have a ledger entry for this peer.
        accepted_receipt_id = body.get("accepted_receipt_id") or tx_hash
        if accepted_receipt_id:
            existing = node.storage.get_receipt(accepted_receipt_id)
            if existing is None:
                logger.warning(
                    f"SETTLEMENT_CONFIRM rejected: no matching pending settlement "
                    f"accepted_receipt_id={accepted_receipt_id[:16]} from={str(from_node)[:16]}"
                )
                return

        await node._enqueue_write(
            node.storage.queue_settlement,
            "settlement_confirmation",
            from_node,
            body,
            0,  # priority
        )
        logger.info(f"SETTLEMENT_CONFIRM from={str(from_node)[:16]} "
                    f"tx={tx_hash[:16]} amount={body['amount_settled']} "
                    f"token={body.get('token', 'KNARR')}")

    async def handle_tab_reminder(item: dict) -> None:
        """Process knarr/commerce/tab_reminder mail."""
        body = _parse_body(item)
        if body is None:
            return

        valid, err = validate_tab_reminder(body)
        if not valid:
            logger.warning(f"Invalid tab_reminder from {item.get('from_node', '?')[:16]}: {err}")
            return

        logger.info(
            f"TAB_REMINDER from={item.get('from_node', '?')[:16]} "
            f"balance={body['current_balance']} utilization_pct={body['utilization_pct']}"
        )

        settlement_cfg = node._get_settlement_config()
        if settlement_cfg.get("tab_reminder_auto_netting", False):
            threshold = settlement_cfg.get("tab_reminder_threshold", 80.0)
            if body["utilization_pct"] >= threshold:
                logger.debug(
                    f"BR_MAIL_001_AUTO_NETTING_TRIGGERED utilization_pct={body['utilization_pct']:.2f}"
                )
                try:
                    await node._run_netting_cycle_if_due()
                except Exception as e:
                    logger.warning(f"BR_MAIL_001_AUTO_NETTING_FAILED error={e!r}")

    # FIX-004/005/006: Netting session store — tracks active proposals by netting_id.
    # Keyed by netting_id; value is from_node and proposal details.
    # F-09: Lock is created lazily inside handlers to avoid binding to the wrong
    # event loop when make_commerce_handlers() is called before a loop is running.
    _netting_sessions: dict = {}
    _netting_lock: asyncio.Lock | None = None

    async def handle_netting_proposal(item: dict) -> None:
        """Process knarr/commerce/netting_proposal mail.

        Validates chain_id against node config.  Rejects proposals for unknown
        or mismatched chains.  Stores session for later acceptance verification.
        """
        body = _parse_body(item)
        if body is None:
            return

        our_chain = node._config.get("blockchain", {}).get("chain", "")
        proposal_chain_id = body.get("chain_id", "")
        if not proposal_chain_id or proposal_chain_id != our_chain:
            logger.warning(
                f"NETTING_PROPOSAL rejected: chain_id mismatch: "
                f"got={proposal_chain_id!r} want={our_chain!r}"
            )
            return

        netting_id = body.get("netting_id")
        if not netting_id:
            logger.warning("NETTING_PROPOSAL rejected: missing netting_id")
            return

        nonlocal _netting_lock
        if _netting_lock is None:
            _netting_lock = asyncio.Lock()

        now = time.time()
        async with _netting_lock:
            # Clean up expired entries
            expired_keys = [k for k, v in _netting_sessions.items() if v.get("expires_at", 0) < now]
            for k in expired_keys:
                # RUL-01: debug log expired netting sessions for convergence diagnosis
                sess = _netting_sessions[k]
                age = now - (sess.get("expires_at", now) - 300)
                logger.debug(
                    "NETTING_SESSION_EXPIRED session_id=%s peer=%s age=%.1fs",
                    k[:16], str(sess.get("from_node", ""))[:16], age,
                )
                del _netting_sessions[k]
            _netting_sessions[netting_id] = {
                "from_node": item.get("from_node"),
                "chain_id": proposal_chain_id,
                "settlement_amount": body.get("settlement_amount"),
                "expires_at": now + 300,
            }
        logger.info(f"NETTING_PROPOSAL netting_id={netting_id[:16]} chain={proposal_chain_id}")

    async def handle_netting_acceptance(item: dict) -> None:
        """Process knarr/commerce/netting_acceptance mail.

        Verifies that a matching netting_proposal session exists.  Rejects
        acceptances that arrive without a prior proposal (replay / forgery guard).
        """
        body = _parse_body(item)
        if body is None:
            return

        netting_id = body.get("netting_id")
        if not netting_id:
            logger.warning("NETTING_ACCEPTANCE rejected: missing netting_id")
            return

        nonlocal _netting_lock
        if _netting_lock is None:
            _netting_lock = asyncio.Lock()

        # F-08: Verify sender BEFORE popping the session.
        # Popping before the check permanently burns the session, letting any peer
        # who guesses a netting_id DoS legitimate settlements.
        async with _netting_lock:
            if netting_id not in _netting_sessions:
                logger.warning(
                    f"NETTING_ACCEPTANCE rejected: no active session for "
                    f"netting_id={netting_id[:16]} (replay or forgery)"
                )
                return
            session = _netting_sessions[netting_id]  # peek — do not pop yet
            if item.get("from_node") != session.get("from_node"):
                logger.warning(
                    f"NETTING_ACCEPTANCE rejected: sender mismatch "
                    f"netting_id={netting_id[:16]} "
                    f"expected={str(session.get('from_node', ''))[:16]} "
                    f"got={str(item.get('from_node', ''))[:16]}"
                )
                return
            # Sender verified — now consume the session
            session = _netting_sessions.pop(netting_id)

        logger.info(
            f"NETTING_ACCEPTANCE accepted netting_id={netting_id[:16]} "
            f"from={item.get('from_node', '?')[:16]}"
        )

    return {
        "knarr/commerce/receipt": handle_receipt,
        "knarr/commerce/credit_note": handle_credit_note,
        "knarr/commerce/settle_request": handle_settle_request,
        "knarr/commerce/settlement_confirmation": handle_settlement_confirmation,
        "knarr/commerce/tab_reminder": handle_tab_reminder,
        "knarr/commerce/netting_proposal": handle_netting_proposal,
        "knarr/commerce/netting_acceptance": handle_netting_acceptance,
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
    """Reverse-lookup: node_id → peer_public_key via indexed storage lookup.

    SEC-01: Uses get_pubkey_by_node_id() for O(1) indexed lookup instead of
    O(n) SHA-256 scan of all ledger entries.
    """
    if not node_id:
        return None
    return node.storage.get_pubkey_by_node_id(node_id)
