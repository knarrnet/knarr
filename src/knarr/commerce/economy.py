"""
Economy summary — B3 balance fields for self/info and cockpit.

Provides structured views of ledger state for the API and cockpit.
Reads new bilateral ledger columns from v0.35.0 migration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PeerEconomy:
    """Economy state for a single peer relationship."""

    peer_key: str
    balance: float
    prepaid: float
    pub_tab: float
    soft_limit: float
    hard_limit: float
    credit_limit: float
    tasks_provided: int
    tasks_consumed: int
    trust: float
    utilization_pct: float  # how much of credit range is used


@dataclass(frozen=True)
class EconomySummary:
    """Aggregate economy state for self/info response."""

    total_peers: int
    active_peers: int  # peers with non-zero balance
    total_balance: float  # sum of all balances (net position)
    total_prepaid: float
    total_pub_tab: float
    total_tasks_provided: int
    total_tasks_consumed: int
    peers_at_soft_limit: int
    peers_at_hard_limit: int


def get_economy_summary(ledger_entries: List[Dict[str, Any]]) -> EconomySummary:
    """Build economy summary from raw ledger rows.

    Pure function: takes ledger rows (dicts), returns summary.
    The caller queries storage and passes rows in.

    Expected dict keys: peer_public_key, balance, prepaid, pub_tab,
    soft_limit, hard_limit, credit_limit, tasks_provided, tasks_consumed, trust
    """
    total_balance = 0.0
    total_prepaid = 0.0
    total_pub_tab = 0.0
    total_provided = 0
    total_consumed = 0
    active = 0
    at_soft = 0
    at_hard = 0

    for row in ledger_entries:
        balance = row.get("balance", 0.0) or 0.0
        prepaid = row.get("prepaid", 0.0) or 0.0
        pub_tab = row.get("pub_tab", 0.0) or 0.0
        soft_limit = row.get("soft_limit", -5.0) or -5.0
        hard_limit = row.get("hard_limit", -10.0) or -10.0

        total_balance += balance
        total_prepaid += prepaid
        total_pub_tab += pub_tab
        total_provided += row.get("tasks_provided", 0)
        total_consumed += row.get("tasks_consumed", 0)

        if balance != 0.0:
            active += 1
        if balance < soft_limit:
            at_soft += 1
        if balance < hard_limit:
            at_hard += 1

    return EconomySummary(
        total_peers=len(ledger_entries),
        active_peers=active,
        total_balance=round(total_balance, 6),
        total_prepaid=round(total_prepaid, 6),
        total_pub_tab=round(total_pub_tab, 6),
        total_tasks_provided=total_provided,
        total_tasks_consumed=total_consumed,
        peers_at_soft_limit=at_soft,
        peers_at_hard_limit=at_hard,
    )


def peer_economy_from_row(row: Dict[str, Any]) -> PeerEconomy:
    """Convert a raw ledger row to a PeerEconomy dataclass."""
    balance = row.get("balance", 0.0) or 0.0
    credit_limit = row.get("credit_limit", 3.0) or 3.0
    soft_limit = row.get("soft_limit", -5.0) or -5.0

    # Utilization: how much of the credit range (credit_limit to soft_limit) is used
    credit_range = credit_limit - soft_limit
    if credit_range <= 0:
        if credit_limit < soft_limit:
            logger.warning(
                f"ECONOMY_INVERTED_LIMITS peer={row.get('peer_public_key', '?')[:16]} "
                f"credit_limit={credit_limit} < soft_limit={soft_limit}"
            )
    if credit_range > 0:
        utilization = ((credit_limit - balance) / credit_range) * 100.0
    else:
        utilization = 0.0

    return PeerEconomy(
        peer_key=row.get("peer_public_key", ""),
        balance=balance,
        prepaid=row.get("prepaid", 0.0),
        pub_tab=row.get("pub_tab", 0.0),
        soft_limit=soft_limit,
        hard_limit=row.get("hard_limit", -10.0),
        credit_limit=credit_limit,
        tasks_provided=row.get("tasks_provided", 0),
        tasks_consumed=row.get("tasks_consumed", 0),
        trust=row.get("trust", 0.3),
        utilization_pct=round(min(max(utilization, 0.0), 100.0), 1),
    )
