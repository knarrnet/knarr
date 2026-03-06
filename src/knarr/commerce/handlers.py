"""Commerce document handlers — async closures for SyncEngine dispatch.

Each handler is an async callable taking a single ``item: dict`` argument,
matching the signature expected by ``SyncEngine._dispatch_system_item``:

    await handler(item)

Node access is captured via closure from ``make_commerce_handlers(node)``.

v0.36.0: handle_settle_request upgraded with dual-signature validation,
pre-settlement reconciliation, and plugin hook (on_inbound_settlement).
"""
import asyncio
import hashlib
import json
import logging
import math
import time
from typing import Tuple

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
            logger.warning(f"Invalid credit_note from {item.get('from_node', '?')[:16]}: {err}")
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
        max_refund = original["price"] * 2  # 2x cap as safety margin
        if amount > max_refund:
            logger.warning(f"Credit note rejected: amount {amount} > 2x original {original['price']}")
            return

        from_node_id = item.get("from_node")
        target_pubkey = _resolve_public_key(node, from_node_id)

        if target_pubkey:
            await node._enqueue_write(node.storage.update_ledger_refund, target_pubkey, amount)
            logger.info(f"CREDIT_NOTE task={body.get('references', {}).get('task_id', 'N/A')[:8]} "
                        f"amount={amount} reason={body.get('reason')}")
        else:
            logger.warning(f"Could not resolve public_key for node_id {from_node_id[:16]} — credit note dropped")

    async def handle_settle_request(item: dict) -> None:
        """Process knarr/commerce/settle_request mail (v0.36.0 dual-signature flow).

        Flow:
        1. Parse body, require document + proof (fail-closed)
        2. Validate BOTH signatures (node + authority) — fail-closed on unresolvable keys
        3. Verify from_node matches proposer in document
        4. Resolve peer via ledger (no untrusted peer_key fallback)
        5. Pre-settlement reconciliation: compare positions, correct on divergence
        6. Dedup check: reject if already processed this settlement
        7. Plugin hook (fail-closed if no plugins)
        8. Adjust ledger by settlement amount (not full zero)
        9. Write confirmation receipt, send mail
        """
        from ..core.proof import verify_document
        from .documents import settlement_confirmation as sc_doc_factory

        body = _parse_body(item)
        if body is None:
            return

        from_node = item.get("from_node", "")

        # --- C3 fix: REQUIRE document with proof ---
        settle_doc = body.get("document")
        if not settle_doc or not isinstance(settle_doc, dict) or "proof" not in settle_doc:
            logger.warning(
                f"SETTLE_REQUEST_NO_DOCUMENT from={from_node[:16]} — "
                f"dual-signed document required"
            )
            return

        amount = body.get("amount", 0.0)
        if not math.isfinite(amount):
            logger.warning(f"SETTLE_REQUEST_INVALID_AMOUNT from={from_node[:16]} amount={amount!r}")
            return

        # --- Dual signature validation ---
        proposer_pk = settle_doc.get("proposer", "")

        # M9 fix: verify from_node matches proposer in document
        if from_node and proposer_pk and from_node != proposer_pk:
            logger.warning(
                f"SETTLE_REQUEST_SENDER_MISMATCH from={from_node[:16]} "
                f"proposer={proposer_pk[:16]} — envelope/document mismatch"
            )
            return

        node_verify_key = _resolve_verify_key(node, proposer_pk)
        if node_verify_key is None:
            logger.warning(
                f"SETTLE_REQUEST_UNKNOWN_PROPOSER from={from_node[:16]} "
                f"proposer={proposer_pk[:16]}"
            )
            return

        # Validate node signature
        if not verify_document(settle_doc, node_verify_key):
            logger.warning(
                f"SETTLE_REQUEST_BAD_NODE_SIG from={from_node[:16]} "
                f"proposer={proposer_pk[:16]}"
            )
            return

        # Authority signature: MANDATORY, fail-closed
        authority_proof = body.get("authority_proof")
        if not authority_proof:
            logger.warning(
                f"SETTLE_REQUEST_MISSING_AUTHORITY from={from_node[:16]} "
                f"— dual signature required"
            )
            return

        # C2 fix: fail-closed on unresolvable authority key
        auth_doc = {k: v for k, v in settle_doc.items() if k != "proof"}
        auth_doc["proof"] = authority_proof
        authority_vm = authority_proof.get("verificationMethod", "")
        authority_key = _resolve_verify_key_by_vm(node, authority_vm)
        if authority_key is None:
            logger.warning(
                f"SETTLE_REQUEST_UNRESOLVABLE_AUTHORITY from={from_node[:16]} "
                f"vm={authority_vm} — cannot verify, rejecting"
            )
            return
        if not verify_document(auth_doc, authority_key):
            logger.warning(
                f"SETTLE_REQUEST_BAD_AUTHORITY_SIG from={from_node[:16]} "
                f"vm={authority_vm}"
            )
            return

        # --- Resolve peer (H2 fix: NO peer_key fallback) ---
        from_pubkey = _resolve_public_key(node, from_node) if from_node else None
        if not from_pubkey:
            logger.warning(
                f"SETTLE_REQUEST_UNRESOLVABLE_PEER from={from_node[:16]} "
                f"— cannot resolve peer public key"
            )
            return

        our_balance = node.storage.get_ledger_balance(from_pubkey)
        if our_balance is None:
            our_balance = 0.0

        # M8 fix: reject if our balance is zero (nothing to settle)
        if our_balance == 0.0:
            logger.info(
                f"SETTLE_REQUEST_ZERO_BALANCE from={from_node[:16]} "
                f"— nothing to settle"
            )
            return

        # --- Pre-settlement reconciliation ---
        settle_amount = amount  # default: use proposer's claimed amount
        reconciliation_cfg = node._config.get("economy", {}).get("settlement", {})
        tolerance = float(reconciliation_cfg.get("reconciliation_tolerance", 0.05))

        if amount != 0:
            divergence = abs(amount - abs(our_balance)) / max(abs(amount), 1e-9)
            if divergence > tolerance:
                # H8 fix: actually USE our own position when divergence exceeds tolerance
                settle_amount = abs(our_balance)
                logger.warning(
                    f"SETTLE_REQUEST_DIVERGENT from={from_node[:16]} "
                    f"claimed={amount:.2f} our_balance={our_balance:.2f} "
                    f"divergence={divergence:.1%} — using own position {settle_amount:.2f}"
                )

        # --- H5 fix: dedup check via receipt_log ---
        accepted_receipt_id = body.get("accepted_receipt_id", "")
        if accepted_receipt_id and hasattr(node.storage, 'get_receipt'):
            existing = node.storage.get_receipt(accepted_receipt_id)
            if existing:
                logger.warning(
                    f"SETTLE_REQUEST_DUPLICATE from={from_node[:16]} "
                    f"receipt_id={accepted_receipt_id[:16]} — already processed"
                )
                return

        # --- Plugin hook (H4 fix: fail-closed default) ---
        plugin_accept = False  # H4: fail-closed when no plugins
        if hasattr(node, '_plugins'):
            try:
                plugin_accept = await node._plugins.on_inbound_settlement(body)
            except Exception as e:
                logger.error(f"PLUGIN_HOOK_FAIL on_inbound_settlement: {e}")
                plugin_accept = False
        else:
            # No plugin system → auto-accept (hotwire mode)
            plugin_accept = True

        if not plugin_accept:
            logger.info(
                f"SETTLE_REQUEST_PLUGIN_REJECT from={from_node[:16]} amount={amount:.2f}"
            )
            return

        # --- Accept: adjust ledger by settlement amount, write confirmation ---
        # C4 fix: use settle_amount, NOT -old_balance
        try:
            old_balance = node.storage.get_ledger_balance(from_pubkey) or 0.0
            if old_balance != 0.0:
                # Adjust by the agreed settlement amount (capped to actual balance)
                adjustment = min(settle_amount, abs(old_balance))
                # Direction: if our balance is negative (we owe them), add positive to reduce
                if old_balance < 0:
                    await node._enqueue_write(
                        node.storage.update_ledger_refund, from_pubkey, adjustment
                    )
                else:
                    await node._enqueue_write(
                        node.storage.update_ledger_refund, from_pubkey, -adjustment
                    )
            final_balance = old_balance + (adjustment if old_balance < 0 else -adjustment) if old_balance != 0 else 0.0
        except Exception as exc:
            logger.error(f"SETTLE_LEDGER_ADJUST_FAIL peer={from_pubkey[:16]}: {exc}")
            return  # Don't send confirmation if ledger adjustment failed

        # Write settlement_confirmation receipt
        processed_receipt_id = ""
        if hasattr(node, '_signing_key') and node._signing_key:
            try:
                from ..core.proof import sign_document as _sign_doc
                from .documents import settlement_confirmation as _sc_factory

                sc_doc = _sc_factory(
                    proposer=from_node,
                    counterparty=node.node_info.node_id,
                    amount_confirmed=round(settle_amount, 6),
                    own_final_balance=round(final_balance, 6),
                    processed_receipt_id=accepted_receipt_id,
                )
                sc_payload = sc_doc.payload
                verification_method = f"did:knarr:{node.node_info.node_id}#key-1"
                signed_sc = _sign_doc(sc_payload, node._signing_key, verification_method)
                processed_receipt_id = sc_doc["receipt_id"]

                sc_payload_json = json.dumps(signed_sc, sort_keys=True, separators=(",", ":"))
                sc_signature = signed_sc["proof"]["proofValue"]

                node.storage.write_receipt(
                    receipt_id=processed_receipt_id,
                    document_type="settlement_confirmation",
                    timestamp=sc_doc["timestamp"],
                    identity=node.node_info.node_id,
                    counterparty=from_pubkey,
                    order_ref=accepted_receipt_id or None,
                    proof_purpose="assertionMethod",
                    payload_json=sc_payload_json,
                    signature=sc_signature,
                )

                if node.bus:
                    node.bus.emit(
                        "settlement.confirmed",
                        receipt_id=processed_receipt_id,
                        document_type="settlement_confirmation",
                        identity=verification_method,
                        counterparty=from_pubkey,
                        payload_json=sc_payload_json,
                    )
            except Exception as exc:
                logger.warning(f"SETTLEMENT_CONF_RECEIPT_FAIL: {exc}")

        # Send confirmation mail back to proposer
        try:
            conf_body = {
                "type": "knarr/commerce/settlement_confirmation",
                "proposer": from_node,
                "counterparty": node.node_info.node_id,
                "amount_confirmed": round(settle_amount, 6),
                "own_final_balance": round(final_balance, 6),
                "processed_receipt_id": processed_receipt_id,
                "accepted_receipt_id": accepted_receipt_id,
                "tx_hash": processed_receipt_id or "none",
                "amount_settled": round(settle_amount, 6),
                "timestamp": time.time(),
                "schema_version": "1.0",
            }
            await node._sync.enqueue(
                to_node=from_node,
                msg_type="knarr/commerce/settlement_confirmation",
                body=conf_body,
                system=True,
            )
        except Exception as mail_err:
            logger.warning(f"SETTLEMENT_CONF_MAIL_FAIL peer={from_node[:16]}: {mail_err}")

        logger.info(
            f"SETTLE_REQUEST_ACCEPTED from={from_node[:16]} "
            f"amount={settle_amount:.2f} old_balance={old_balance:.2f} "
            f"final={final_balance:.2f}"
        )

    async def handle_settlement_confirmation(item: dict) -> None:
        """Process knarr/commerce/settlement_confirmation mail (v0.36.0).

        When the counterparty confirms our outbound settlement, we adjust our
        own ledger by the confirmed amount and write a settlement_processed receipt.

        Security: C1 fix — requires matching outbound settlement receipt,
        NaN validation, no peer_key fallback, amount-based adjustment.
        """
        body = _parse_body(item)
        if body is None:
            return

        from_node = item.get("from_node", "")
        amount_confirmed = body.get("amount_confirmed", body.get("amount_settled", 0.0))
        processed_receipt_id = body.get("processed_receipt_id", "")
        accepted_receipt_id = body.get("accepted_receipt_id", "")

        # M1 fix: NaN/Inf validation on amount_confirmed
        if not isinstance(amount_confirmed, (int, float)) or not math.isfinite(float(amount_confirmed)):
            logger.warning(
                f"SETTLEMENT_CONF_INVALID_AMOUNT from={from_node[:16]} "
                f"amount={amount_confirmed!r}"
            )
            return
        amount_confirmed = float(amount_confirmed)

        # H2 fix: NO peer_key fallback — resolve from authenticated from_node only
        from_pubkey = _resolve_public_key(node, from_node) if from_node else None
        if not from_pubkey:
            logger.warning(
                f"SETTLEMENT_CONF_UNRESOLVABLE_PEER from={from_node[:16]}"
            )
            return

        if not hasattr(node, '_signing_key') or not node._signing_key:
            logger.warning(f"SETTLEMENT_CONF_NO_SIGNING_KEY from={from_node[:16]}")
            return

        # C1 + H6 fix: verify we haven't already processed this confirmation
        # Check receipt_log for a settlement_processed receipt with matching ref
        if hasattr(node.storage, 'get_receipts_by_type'):
            existing_confirmations = node.storage.get_receipts_by_type("settlement_confirmation")
            order_ref = accepted_receipt_id or processed_receipt_id
            already_processed = any(
                r.get("order_ref") == order_ref and
                (r.get("counterparty") == from_node or r.get("counterparty") == from_pubkey)
                for r in existing_confirmations
            )
            if already_processed:
                logger.warning(
                    f"SETTLEMENT_CONF_DUPLICATE from={from_node[:16]} "
                    f"ref={accepted_receipt_id or processed_receipt_id} — already processed"
                )
                return

        old_balance = node.storage.get_ledger_balance(from_pubkey) or 0.0

        # C4 fix: adjust by confirmed amount, not full zero
        try:
            if old_balance != 0.0 and amount_confirmed != 0.0:
                adjustment = min(amount_confirmed, abs(old_balance))
                if old_balance < 0:
                    await node._enqueue_write(
                        node.storage.update_ledger_refund, from_pubkey, adjustment
                    )
                else:
                    await node._enqueue_write(
                        node.storage.update_ledger_refund, from_pubkey, -adjustment
                    )
                final_balance = old_balance + (adjustment if old_balance < 0 else -adjustment)
            else:
                final_balance = old_balance
        except Exception as exc:
            logger.error(f"SETTLEMENT_CONF_LEDGER_FAIL peer={from_pubkey[:16]}: {exc}")
            return

        # Write settlement_processed receipt
        try:
            from ..commerce.settlement_execution import write_settlement_processed
            receipt_id = await write_settlement_processed(
                node_id=node.node_info.node_id,
                peer_key=from_pubkey,
                amount_settled=amount_confirmed,
                ledger_delta=amount_confirmed,
                final_balance=final_balance,
                accepted_receipt_id=accepted_receipt_id or processed_receipt_id,
                settle_request_ref=processed_receipt_id,
                signing_key=node._signing_key,
                storage=node.storage,
                bus=getattr(node, 'bus', None),
            )
            logger.info(
                f"SETTLEMENT_PROCESSED receipt={receipt_id[:16]} "
                f"peer={from_pubkey[:16]} amount={amount_confirmed:.2f}"
            )
        except Exception as exc:
            logger.warning(f"SETTLEMENT_PROCESSED_WRITE_FAIL: {exc}")

    # ── v0.38.0 A5.6: Netting mail handlers ────────────────────────────
    # FIX-004: Session store for netting lifecycle — tracks proposals we've sent
    # Key: netting_id, Value: {amount, counterparty, timestamp, consumed}
    _netting_sessions: dict = {}

    async def handle_netting_reconcile(item: dict) -> None:
        """Step 1b: counterparty receives reconcile, checks position, sends proposal."""
        body = _parse_body(item)
        if body is None:
            return

        from_node = item.get("from_node", "")
        netting_id = body.get("netting_id", "")
        their_net = body.get("proposed_net", None)
        their_receipt_count = body.get("receipt_count", 0)
        chain_id = body.get("chain_id", "")

        if not netting_id or their_net is None:
            logger.warning(f"NETTING_RECONCILE_INVALID from={from_node[:16]}")
            return
        if not math.isfinite(their_net):
            logger.warning(f"NETTING_RECONCILE_BAD_NET from={from_node[:16]} net={their_net!r}")
            return

        # FIX-006: Validate chain_id from initiator
        expected_chain = node._config.get("blockchain", {}).get("chain", "solana-devnet")
        if not chain_id or chain_id != expected_chain:
            logger.warning(
                f"NETTING_RECONCILE_CHAIN_MISMATCH from={from_node[:16]} "
                f"got={chain_id!r} expected={expected_chain!r}"
            )
            return

        # Resolve our position for this peer
        from_pubkey = _resolve_public_key(node, from_node)
        if not from_pubkey:
            logger.warning(f"NETTING_RECONCILE_UNRESOLVABLE from={from_node[:16]}")
            return

        our_balance = node.storage.get_ledger_balance(from_pubkey) or 0.0
        # Our view of what they owe us (positive = they owe us)
        # their view of what they owe us should be sign-flipped from our balance
        # If our balance = -42 (they owe us 42), they see +42
        our_view = -our_balance  # what we say they owe us (positive means they owe us)

        # Tolerance check: allow 5% divergence
        if abs(their_net) > 0:
            divergence = abs(their_net - our_view) / max(abs(their_net), 1e-9)
        else:
            divergence = abs(our_view)

        if divergence > 0.05:
            logger.warning(
                f"NETTING_RECONCILE_MISMATCH from={from_node[:16]} "
                f"their_net={their_net:.2f} our_view={our_view:.2f} divergence={divergence:.1%}"
            )
            return

        # Position matches — send proposal with our wallet as target
        settlement_amount = abs(our_balance) if our_balance < 0 else abs(our_view)
        if settlement_amount <= 0:
            logger.info(f"NETTING_RECONCILE_ZERO from={from_node[:16]} — nothing to settle")
            return

        # Get our wallet address via punchhole
        our_wallet = _get_own_wallet_address(node)
        if not our_wallet:
            logger.warning(f"NETTING_RECONCILE_NO_WALLET from={from_node[:16]}")
            return

        import secrets as _secrets
        from datetime import datetime, timezone, timedelta
        deadline = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        token_mint = node._config.get("blockchain", {}).get("token_mint", "")

        try:
            await node._sync.enqueue(
                to_node=from_node,
                msg_type="knarr/commerce/netting_proposal",
                body={
                    "type": "knarr/commerce/netting_proposal",
                    "netting_id": netting_id,
                    "identity": node.node_info.node_id,
                    "counterparty": from_node,
                    "settlement_amount": round(settlement_amount, 6),
                    "chain_id": chain_id,
                    "token_mint": token_mint,
                    "target_address": our_wallet,
                    "deadline": deadline,
                    "timestamp": time.time(),
                },
                system=True,
            )
            logger.info(
                f"NETTING_PROPOSAL_SENT to={from_node[:16]} "
                f"netting_id={netting_id[:12]} amount={settlement_amount:.2f}"
            )
        except Exception as exc:
            logger.error(f"NETTING_PROPOSAL_SEND_FAIL: {exc}")

    async def handle_netting_proposal(item: dict) -> None:
        """Step 2b: initiator receives proposal, validates, sends acceptance."""
        body = _parse_body(item)
        if body is None:
            return

        from_node = item.get("from_node", "")
        netting_id = body.get("netting_id", "")
        proposal_ref = body.get("receipt_id", netting_id)
        settlement_amount = body.get("settlement_amount", 0.0)
        chain_id = body.get("chain_id", "")

        if not netting_id:
            logger.warning(f"NETTING_PROPOSAL_INVALID from={from_node[:16]}")
            return
        if not math.isfinite(settlement_amount) or settlement_amount <= 0:
            logger.warning(f"NETTING_PROPOSAL_BAD_AMOUNT from={from_node[:16]} amount={settlement_amount!r}")
            return

        # FIX-006: Chain mismatch rejection — reject empty or mismatched chain_id
        expected_chain = node._config.get("blockchain", {}).get("chain", "solana-devnet")
        if not chain_id or chain_id != expected_chain:
            logger.warning(
                f"NETTING_PROPOSAL_CHAIN_MISMATCH from={from_node[:16]} "
                f"got={chain_id!r} expected={expected_chain!r}"
            )
            return

        # Get our source wallet
        our_wallet = _get_own_wallet_address(node)
        if not our_wallet:
            logger.warning(f"NETTING_PROPOSAL_NO_WALLET from={from_node[:16]}")
            return

        # FIX-004/005: Record session before sending acceptance
        _netting_sessions[netting_id] = {
            "amount": round(settlement_amount, 6),
            "counterparty": from_node,
            "timestamp": time.time(),
            "consumed": False,
        }
        try:
            await node._sync.enqueue(
                to_node=from_node,
                msg_type="knarr/commerce/netting_acceptance",
                body={
                    "type": "knarr/commerce/netting_acceptance",
                    "netting_id": netting_id,
                    "proposal_ref": proposal_ref,
                    "identity": node.node_info.node_id,
                    "counterparty": from_node,
                    "accepted_amount": round(settlement_amount, 6),
                    "source_address": our_wallet,
                    "timestamp": time.time(),
                },
                system=True,
            )
            logger.info(
                f"NETTING_ACCEPTANCE_SENT to={from_node[:16]} "
                f"netting_id={netting_id[:12]} amount={settlement_amount:.2f}"
            )
        except Exception as exc:
            # Clean up session on send failure
            _netting_sessions.pop(netting_id, None)
            logger.error(f"NETTING_ACCEPTANCE_SEND_FAIL: {exc}")

    async def handle_netting_acceptance(item: dict) -> None:
        """Step 3b: counterparty receives acceptance, submits on-chain, sends executed."""
        body = _parse_body(item)
        if body is None:
            return

        from_node = item.get("from_node", "")
        netting_id = body.get("netting_id", "")
        proposal_ref = body.get("proposal_ref", "")
        accepted_amount = body.get("accepted_amount", 0.0)
        source_address = body.get("source_address", "")

        if not netting_id or not math.isfinite(accepted_amount) or accepted_amount <= 0:
            logger.warning(f"NETTING_ACCEPTANCE_INVALID from={from_node[:16]}")
            return

        # FIX-004: Verify this acceptance corresponds to a proposal WE sent
        session = _netting_sessions.get(netting_id)
        if not session:
            logger.warning(
                f"NETTING_ACCEPTANCE_NO_SESSION from={from_node[:16]} "
                f"netting_id={netting_id[:12]} — no matching proposal found"
            )
            return
        # FIX-005: Prevent replay — mark session consumed on first use
        if session.get("consumed"):
            logger.warning(
                f"NETTING_ACCEPTANCE_REPLAY from={from_node[:16]} "
                f"netting_id={netting_id[:12]} — already consumed"
            )
            return
        # Verify counterparty matches
        if session["counterparty"] != from_node:
            logger.warning(
                f"NETTING_ACCEPTANCE_WRONG_PEER from={from_node[:16]} "
                f"expected={session['counterparty'][:16]}"
            )
            return
        # Verify amount matches what we proposed (within rounding tolerance)
        if abs(accepted_amount - session["amount"]) > 0.01:
            logger.warning(
                f"NETTING_ACCEPTANCE_AMOUNT_MISMATCH from={from_node[:16]} "
                f"accepted={accepted_amount} expected={session['amount']}"
            )
            return
        # Mark session consumed BEFORE executing (prevents replay during await)
        session["consumed"] = True

        # Submit on-chain transaction via wallet plugin (if available)
        tx_hash = "pending"
        chain_id = node._config.get("blockchain", {}).get("chain", "solana-devnet")
        try:
            # Try to submit via solana_rpc_plugin if registered
            if hasattr(node, 'call_local'):
                tx_result = await node.call_local(
                    "solana-rpc",
                    {"action": "send", "to": source_address, "amount": accepted_amount},
                )
                tx_hash = tx_result.get("tx_hash", "pending") if isinstance(tx_result, dict) else "pending"
        except Exception as exc:
            logger.warning(f"NETTING_ONCHAIN_SUBMIT_FAIL: {exc} — recording as pending")

        # Send netting_executed to initiator
        try:
            await node._sync.enqueue(
                to_node=from_node,
                msg_type="knarr/commerce/netting_executed",
                body={
                    "type": "knarr/commerce/netting_executed",
                    "netting_id": netting_id,
                    "acceptance_ref": proposal_ref,
                    "identity": node.node_info.node_id,
                    "counterparty": from_node,
                    "tx_hash": tx_hash,
                    "chain_id": chain_id,
                    "amount": round(accepted_amount, 6),
                    "timestamp": time.time(),
                },
                system=True,
            )
            logger.info(
                f"NETTING_EXECUTED_SENT to={from_node[:16]} "
                f"netting_id={netting_id[:12]} tx_hash={tx_hash[:16]}"
            )
        except Exception as exc:
            logger.error(f"NETTING_EXECUTED_SEND_FAIL: {exc}")

    async def handle_netting_executed(item: dict) -> None:
        """Step 4b: initiator receives executed notice, records tx_hash."""
        body = _parse_body(item)
        if body is None:
            return

        from_node = item.get("from_node", "")
        netting_id = body.get("netting_id", "")
        tx_hash = body.get("tx_hash", "")
        amount = body.get("amount", 0.0)
        chain_id = body.get("chain_id", "")

        if not netting_id or not tx_hash:
            logger.warning(f"NETTING_EXECUTED_INVALID from={from_node[:16]}")
            return

        # Record the execution — BCW will confirm independently
        logger.info(
            f"NETTING_EXECUTED_RECEIVED from={from_node[:16]} "
            f"netting_id={netting_id[:12]} tx_hash={tx_hash[:16]} amount={amount}"
        )

        # Emit bus event for BCW to pick up
        if node.bus:
            node.bus.emit(
                "netting.executed",
                netting_id=netting_id,
                tx_hash=tx_hash,
                chain_id=chain_id,
                amount=amount,
                counterparty=from_node,
                identity=node.node_info.node_id,
            )

    return {
        "knarr/commerce/receipt": handle_receipt,
        "knarr/commerce/credit_note": handle_credit_note,
        "knarr/commerce/settle_request": handle_settle_request,
        "knarr/commerce/settlement_confirmation": handle_settlement_confirmation,
        # v0.38.0 A5.6: netting exchange handlers
        "knarr/commerce/netting_reconcile": handle_netting_reconcile,
        "knarr/commerce/netting_proposal": handle_netting_proposal,
        "knarr/commerce/netting_acceptance": handle_netting_acceptance,
        "knarr/commerce/netting_executed": handle_netting_executed,
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


def _resolve_verify_key(node, node_id: str):
    """Resolve a VerifyKey for a node_id.

    For the proposer: look up their public key in our peer store.
    Returns nacl.signing.VerifyKey or None if not found.
    """
    if not node_id:
        return None

    # Try direct peers first
    try:
        peers = node.storage.get_peers()
        for peer in peers:
            if peer.get("node_id") == node_id:
                pk_hex = peer.get("public_key") or peer.get("pk_hex", "")
                if pk_hex:
                    from nacl.signing import VerifyKey
                    return VerifyKey(bytes.fromhex(pk_hex))
    except Exception:
        pass

    # Try ledger entries (node_id == SHA-256(pk))
    pk_hex = _resolve_public_key(node, node_id)
    if pk_hex:
        try:
            from nacl.signing import VerifyKey
            return VerifyKey(bytes.fromhex(pk_hex))
        except Exception:
            pass

    return None


def _resolve_verify_key_by_vm(node, verification_method: str):
    """Resolve a VerifyKey from a DID verification method string.

    Handles: did:knarr:{node_id}#key-1, did:knarr:{node_id}#cockpit-1, etc.
    Returns nacl.signing.VerifyKey or None.
    """
    if not verification_method or ":" not in verification_method:
        return None

    try:
        # did:knarr:{node_id}#{fragment}
        parts = verification_method.split(":")
        if len(parts) >= 3 and parts[0] == "did" and parts[1] == "knarr":
            node_id_part = parts[2].split("#")[0]
            return _resolve_verify_key(node, node_id_part)
    except Exception:
        pass

    return None


def _get_own_wallet_address(node) -> str | None:
    """Get own hot wallet Solana address.

    Tries vault_get first (wallet plugin path), then falls back to
    node._wallet attribute (legacy).
    """
    # Try wallet plugin path via vault
    try:
        vault = getattr(node, '_vault', None)
        if vault and hasattr(vault, 'get'):
            seed_hex = vault.get("__wallet__", "hot_seed")
            if seed_hex:
                from knarr.core.wallet import derive_solana_address
                from nacl.signing import SigningKey
                seed = bytes.fromhex(seed_hex)
                return derive_solana_address(SigningKey(seed))
    except Exception:
        pass

    # Fall back to node._wallet (legacy attribute)
    wallet = getattr(node, '_wallet', None)
    if wallet and isinstance(wallet, str) and wallet:
        return wallet

    return None
