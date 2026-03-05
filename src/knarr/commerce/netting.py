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
