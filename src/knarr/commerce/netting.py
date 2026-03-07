"""Treasury Netting Cycle."""
import logging
import time

logger = logging.getLogger("knarr.commerce.netting")

def run_netting_cycle(node) -> int:
    """Check all bilateral positions against soft threshold. Returns count of new settlement orders queued."""
    config = getattr(node, "_config", {}).get("settlement", {})
    soft_threshold = config.get("soft_threshold", 0.8)  # 80% of credit limit
    soft_target = config.get("soft_target", 0.5)         # settle down to 50%
    min_amount = config.get("min_settlement_amount", 10.0)  # don't settle < 10 KNARR

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

        utilization = (ic - balance) / credit_range
        if utilization < soft_threshold:
            continue

        # Calculate settlement amount to reach soft_target
        target_balance = ic - (soft_target * credit_range)
        settle_amount = target_balance - balance
        if settle_amount < min_amount:
            continue

        # Check: don't queue if there's already a pending settlement for this peer
        if node.storage.has_pending_settlement(pk):
            continue

        # Queue soft-threshold settlement order
        node.storage.queue_settlement(
            item_type="soft_threshold",
            from_node=node.node_info.node_id,  # self-generated
            body={
                "type": "netting_trigger",
                "peer_public_key": pk,
                "current_balance": round(balance, 2),
                "utilization_pct": round(utilization * 100, 1),
                "settle_amount": round(settle_amount, 2),
                "target_balance": round(target_balance, 2),
                "timestamp": time.time(),
            },
            priority=2,  # soft threshold = priority 2 (below hard limit)
        )
        queued += 1

    if queued:
        logger.info(f"NETTING_CYCLE queued={queued} settlements")
    return queued
