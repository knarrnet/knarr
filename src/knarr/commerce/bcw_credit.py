"""BCW on-chain payment credit handler.

CR-01: Subscribes to `payment.finalized.*` bus events emitted by the BCW plugin.
When an on-chain payment confirms, resolves the sending wallet to a node_id,
then credits the bilateral ledger.

Design constraints:
- Must not bypass the settlement write path (Elder mandate)
- Handles gracefully if wallet address cannot be resolved (log warning, skip credit)
- Emits `settlement.credit_applied` bus event after credit
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Optional

from .conversion import get_conversion_rate, token_to_credits

logger = logging.getLogger(__name__)


def make_payment_finalized_handler(node):
    """Return an async handler for payment.finalized.* bus events.

    Called once at node startup to bind a handler that closes over the node.
    """
    async def _handle_payment_finalized(event: dict) -> None:
        """Credit ledger when an on-chain payment.finalized.* event arrives."""
        event_type = event.get("event", "")
        if not event_type.startswith("payment.finalized."):
            return

        # Guard against spoofed mint — check configured mint before crediting
        expected_mint = str(
            (getattr(node, '_config', None) or {}).get("economy", {}).get("knarr_mint", "")
        )
        if expected_mint:
            event_mint = str(
                event.get("mint_address") or event.get("mint") or event.get("token_mint") or ""
            )
            if event_mint != expected_mint:
                logger.warning(
                    f"BCW_CREDIT_SKIP_WRONG_MINT event={event_type} "
                    f"tx={str(event.get('tx_hash', ''))[:16]} "
                    f"expected={expected_mint[:16]} got={event_mint[:16]}"
                )
                return

        from_address = str(event.get("from_address", "") or "")
        raw_amount = event.get("amount", 0)
        chain_id = str(event.get("chain_id", "") or "")
        tx_hash = str(event.get("tx_hash", "") or "")
        decimals_raw = event.get("decimals")
        decimals = int(decimals_raw) if decimals_raw is not None else 9

        # Validate raw amount — must be a finite positive number
        try:
            raw_amount = float(raw_amount)
        except (TypeError, ValueError):
            logger.warning(
                f"BCW_CREDIT_SKIP event={event_type} tx={tx_hash[:16]} "
                f"reason=non_numeric_amount raw={raw_amount!r}"
            )
            return

        if not math.isfinite(raw_amount) or raw_amount <= 0:
            logger.warning(
                f"BCW_CREDIT_SKIP event={event_type} tx={tx_hash[:16]} "
                f"reason=invalid_amount raw={raw_amount!r}"
            )
            return

        # Dedup — reject if this tx_hash has already been credited
        dedup_receipt_id = f"bcw_credit:{chain_id}:{tx_hash}"
        existing = node.storage.get_receipt(dedup_receipt_id)
        if existing is not None:
            if node._debug:
                logger.info(
                    f"BCW_CREDIT_SKIP_DEDUP event={event_type} "
                    f"tx={tx_hash[:16]} reason=already_credited"
                )
            return

        # Convert raw on-chain units (lamports / smallest-unit) → tokens → credits
        token_amount = raw_amount / (10 ** decimals)
        try:
            config = getattr(node, '_config', None) or {}
            rate = get_conversion_rate(config)
            amount_credits = token_to_credits(token_amount, rate)
        except ValueError as exc:
            logger.warning(
                f"BCW_CREDIT_CONVERSION_FAIL event={event_type} tx={tx_hash[:16]}: {exc}"
            )
            return

        # Resolve wallet address → node_id
        node_id: Optional[str] = node.storage.get_node_by_wallet(from_address)
        if not node_id:
            logger.warning(
                f"BCW_CREDIT_UNRESOLVED event={event_type} "
                f"from_address={from_address[:16]} tx={tx_hash[:16]} "
                f"reason=wallet_not_in_address_book"
            )
            return

        # Resolve node_id → peer public key (for ledger keying)
        peer_key: Optional[str] = node.storage.get_pubkey_by_node_id(node_id)
        if not peer_key:
            logger.warning(
                f"BCW_CREDIT_NO_PUBKEY event={event_type} "
                f"node_id={node_id[:16]} tx={tx_hash[:16]} "
                f"reason=pubkey_not_found"
            )
            return

        # Write dedup receipt BEFORE applying credit (prevents double-credit on restart)
        node.storage.write_receipt(
            receipt_id=dedup_receipt_id,
            document_type="bcw_payment_credit",
            timestamp=str(time.time()),
            identity=node.node_info.node_id,
            counterparty=peer_key,
            order_ref=tx_hash or None,
            proof_purpose="creditApplied",
            payload_json=(
                f'{{"chain_id":"{chain_id}","tx_hash":"{tx_hash}",'
                f'"raw_amount":{raw_amount},"decimals":{decimals},'
                f'"amount_credits":{amount_credits:.6f}}}'
            ),
            signature=None,
        )

        # Credit ledger through the settlement write path (Elder mandate).
        # On-chain payment = they paid us = apply positive credit (no task counters).
        await node._enqueue_write(
            node.storage.credit_ledger, peer_key, amount_credits
        )

        logger.info(
            f"BCW_CREDIT_APPLIED event={event_type} node_id={node_id[:16]} "
            f"peer_key={peer_key[:16]} raw={raw_amount} decimals={decimals} "
            f"credits={amount_credits:.4f} chain={chain_id} tx={tx_hash[:16]}"
        )

        # Emit settlement.credit_applied bus event
        if node.bus:
            node.bus.emit(
                "settlement.credit_applied",
                amount=amount_credits,
                chain_id=chain_id,
                node_id=node_id,
                tx_hash=tx_hash,
                identity=node.node_info.node_id,
            )

        if node._debug:
            logger.info(
                f"BCW_CREDIT_EMITTED settlement.credit_applied "
                f"node_id={node_id[:16]} credits={amount_credits:.4f}"
            )

    return _handle_payment_finalized
