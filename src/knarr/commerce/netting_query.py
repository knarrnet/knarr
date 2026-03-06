"""
Netting query — A5.4.

query() inspects bilateral positions and optionally runs the settlement
engine to produce settlement proposals.

Three scopes:
    node   — single named peer (target = peer_public_key)
    felag  — a named group of peers (target = group name)
    book   — full ledger (all peers)

raw=True   → bare position dicts
raw=False  → run evaluate_settlement(), return proposals where action == "settle"

Cockpit endpoint:
    GET /api/netting/query?scope=book&raw=false
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def query(
    storage,
    scope: str,
    target: Optional[str],
    raw: bool,
    config: Dict[str, Any],
    resolve_policy_fn: Optional[Callable[[str], Any]] = None,
) -> List[Dict[str, Any]]:
    """Query bilateral positions with optional settlement evaluation.

    Args:
        storage:           Storage instance with get_all_ledger_entries().
        scope:             "node" | "felag" | "book"
        target:            peer_public_key (node), group name (felag), or None (book).
        raw:               If True, return bare position dicts. If False, run engine.
        config:            Full node config dict.
        resolve_policy_fn: Optional callable(peer_key) -> (credit_limit, hard_limit).
                           Used when raw=False to build SettlementInput. If None,
                           defaults from config are used.

    Returns:
        List of result dicts. Format depends on raw:
            raw=True:  [{peer_key, balance, prepaid, pub_tab, ...}, ...]
            raw=False: [{peer_key, action, amount, reason, utilization}, ...]
    """
    scope = scope.strip().lower() if scope else "book"
    entries = storage.get_all_ledger_entries()

    # Filter by scope
    if scope == "node":
        if not target:
            logger.warning("NETTING_QUERY scope=node requires target peer_key")
            return []
        entries = [e for e in entries if e.get("peer_public_key") == target]
    elif scope == "felag":
        if not target:
            logger.warning("NETTING_QUERY scope=felag requires target group name")
            return []
        # Resolve group members from config [groups.<name>.members]
        group_cfg = config.get("groups", {}).get(target, {})
        members = set(group_cfg.get("members", []))
        if not members:
            logger.warning(f"NETTING_QUERY felag={target!r} has no members in config")
            return []
        entries = [e for e in entries if e.get("peer_public_key") in members]
    elif scope == "book":
        pass  # all entries
    else:
        logger.warning(f"NETTING_QUERY unknown scope={scope!r} — defaulting to book")

    if raw:
        return _raw_positions(entries)
    return _evaluated_positions(entries, config, resolve_policy_fn)


def _raw_positions(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return bare position dicts, one per ledger entry."""
    results = []
    for e in entries:
        balance = e.get("balance", 0.0) or 0.0
        hard_limit = e.get("hard_limit", -10.0) or -10.0
        if hard_limit != 0:
            utilization = abs(min(balance, 0.0)) / abs(hard_limit) * 100.0
        else:
            utilization = 0.0
        results.append({
            "peer_key": e.get("peer_public_key", ""),
            "balance": balance,
            "prepaid": e.get("prepaid", 0.0) or 0.0,
            "pub_tab": e.get("pub_tab", 0.0) or 0.0,
            "soft_limit": e.get("soft_limit", -5.0) or -5.0,
            "hard_limit": hard_limit,
            "credit_limit": e.get("credit_limit", 3.0) or 3.0,
            "tasks_provided": e.get("tasks_provided", 0),
            "tasks_consumed": e.get("tasks_consumed", 0),
            "utilization_pct": round(min(max(utilization, 0.0), 100.0), 1),
        })
    return results


def _evaluated_positions(
    entries: List[Dict[str, Any]],
    config: Dict[str, Any],
    resolve_policy_fn: Optional[Callable[[str], Any]],
) -> List[Dict[str, Any]]:
    """Run settlement engine on each entry; return only 'settle' decisions."""
    from .settlement_engine import SettlementInput, evaluate_settlement

    settle_cfg = config.get("economy", {}).get("settlement", {})
    results = []

    for e in entries:
        peer_key = e.get("peer_public_key", "")
        balance = e.get("balance", 0.0) or 0.0
        prepaid = e.get("prepaid", 0.0) or 0.0
        pub_tab = e.get("pub_tab", 0.0) or 0.0

        # Resolve limits
        if resolve_policy_fn:
            try:
                credit_limit, hard_limit = resolve_policy_fn(peer_key)
            except Exception:
                credit_limit = e.get("credit_limit", 3.0) or 3.0
                hard_limit = e.get("hard_limit", -10.0) or -10.0
        else:
            credit_limit = e.get("credit_limit", 3.0) or 3.0
            hard_limit = e.get("hard_limit", -10.0) or -10.0

        soft_limit = e.get("soft_limit", -5.0) or -5.0

        # Compute utilization using new formula
        if hard_limit < 0:
            utilization = abs(min(balance, 0.0)) / abs(hard_limit)
        else:
            utilization = 0.0

        if not math.isfinite(utilization):
            utilization = 0.0

        inp = SettlementInput(
            peer_key=peer_key,
            balance=balance,
            prepaid=prepaid,
            pub_tab=pub_tab,
            soft_limit=soft_limit,
            hard_limit=hard_limit,
            credit_limit=credit_limit,
            tasks_provided=e.get("tasks_provided", 0),
            tasks_consumed=e.get("tasks_consumed", 0),
            utilization=utilization,
        )

        out = evaluate_settlement(inp, settle_cfg)

        results.append({
            "peer_key": peer_key,
            "action": out.action,
            "amount": out.amount,
            "reason": out.reason,
            "utilization_pct": round(min(max(utilization * 100.0, 0.0), 100.0), 1),
            "target_utilization": out.target_utilization,
            "balance": balance,
        })

    return results
