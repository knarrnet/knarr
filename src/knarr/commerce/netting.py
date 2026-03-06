"""Treasury Netting Cycle.

v0.37.0: Delegates settlement evaluation to settlement_engine.evaluate_settlement()
(P-010 pure function) instead of inline logic. Removes code duplication.
"""
import logging
import time

from .settlement_engine import SettlementInput, evaluate_settlement

logger = logging.getLogger("knarr.commerce.netting")


def run_netting_cycle(node) -> int:
    """Check all bilateral positions against soft threshold. Returns count queued."""
    config = getattr(node, "_config", {}).get("settlement", {})
    entries = node.storage.get_all_ledger_entries()
    queued = 0

    for entry in entries:
        pk = entry["peer_public_key"]
        balance = entry["balance"]

        # Resolve credit policy for this peer
        ic, mb = node._resolve_policy(pk, "")
        credit_range = ic - mb
        if credit_range <= 0:
            continue

        utilization = abs(balance) / abs(mb) if mb != 0 else 0.0

        # Delegate decision to settlement_engine (P-010 pure function)
        inp = SettlementInput(
            peer_key=pk,
            balance=balance,
            prepaid=entry.get("prepaid", 0.0),
            pub_tab=entry.get("pub_tab", 0.0),
            soft_limit=entry.get("soft_limit", -5.0),
            hard_limit=mb,
            credit_limit=ic,
            tasks_provided=entry.get("tasks_provided", 0),
            tasks_consumed=entry.get("tasks_consumed", 0),
            utilization=utilization,
        )
        result = evaluate_settlement(inp, config)

        if result.action != "settle":
            continue

        # Check: don't queue if there's already a pending settlement
        if node.storage.has_pending_settlement(pk):
            continue

        # Queue soft-threshold settlement order
        node.storage.queue_settlement(
            item_type="soft_threshold",
            from_node=node.node_info.node_id,
            body={
                "type": "netting_trigger",
                "peer_public_key": pk,
                "current_balance": round(balance, 2),
                "utilization_pct": round(utilization * 100, 1),
                "settle_amount": round(result.amount, 2),
                "target_utilization": round(result.target_utilization, 2),
                "reason": result.reason,
                "timestamp": time.time(),
            },
            priority=2,
        )
        queued += 1

    if queued:
        logger.info(f"NETTING_CYCLE queued={queued} settlements")
    return queued


async def initiate_netting(node, peer_key: str, chain_config: dict) -> bool:
    """A5.6: Initiate a netting exchange with a specific peer.

    Step 1 of the 5-step netting protocol: send netting_reconcile message.

    Args:
        node:         The KnarrNode instance.
        peer_key:     Peer public key (hex).
        chain_config: Chain config dict from get_chain_config().

    Returns:
        True if reconcile was sent successfully.
    """
    import secrets as _secrets

    balance = node.storage.get_ledger_balance(peer_key) or 0.0
    if balance == 0.0:
        logger.info(f"NETTING_INITIATE_SKIP peer={peer_key[:16]} balance=0.0")
        return False

    # Count receipts for this peer (approximate via tasks provided+consumed)
    entries = node.storage.get_all_ledger_entries()
    receipt_count = 0
    for e in entries:
        if e.get("peer_public_key") == peer_key:
            receipt_count = e.get("tasks_provided", 0) + e.get("tasks_consumed", 0)
            break

    # netting_id is a unique per-session ID
    netting_id = _secrets.token_hex(16)
    chain_id = chain_config.get("chain_id", "solana-devnet")

    # proposed_net: what WE say THEY owe us (sign: positive = they owe us)
    # Our balance is negative when they owe us → proposed_net = -balance
    proposed_net = -balance

    # Resolve counterparty node_id (SHA-256 of peer_key)
    import hashlib as _hashlib
    counterparty_node_id = _hashlib.sha256(bytes.fromhex(peer_key)).hexdigest()

    try:
        await node._sync.enqueue(
            to_node=counterparty_node_id,
            msg_type="knarr/commerce/netting_reconcile",
            body={
                "type": "knarr/commerce/netting_reconcile",
                "netting_id": netting_id,
                "identity": node.node_info.node_id,
                "counterparty": counterparty_node_id,
                "proposed_net": round(proposed_net, 6),
                "receipt_count": receipt_count,
                "chain_id": chain_id,
                "timestamp": time.time(),
            },
            system=True,
        )
        logger.info(
            f"NETTING_RECONCILE_SENT to={counterparty_node_id[:16]} "
            f"netting_id={netting_id[:12]} proposed_net={proposed_net:.2f}"
        )
        return True
    except Exception as exc:
        logger.error(f"NETTING_INITIATE_FAIL peer={peer_key[:16]}: {exc}")
        return False
