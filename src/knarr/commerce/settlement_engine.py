"""
Settlement engine module — P-010 pattern (frozen dataclass in, frozen dataclass out).

Determines whether to settle with a peer and for how much based on utilization thresholds.
Pure function: no side effects, no I/O. The caller handles storage, signing, and mail.

Config keys (from [economy.settlement]):
    - soft_threshold: utilization threshold to trigger settlement (default: 0.8)
    - soft_target: target utilization after settlement (default: 0.5)
    - min_settlement_amount: minimum amount to settle (default: 10.0)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettlementInput:
    """Input to the settlement engine for a single peer."""

    peer_key: str
    balance: float              # net position (positive = they owe us, negative = we owe them)
    prepaid: float
    pub_tab: float
    soft_limit: float
    hard_limit: float
    credit_limit: float
    tasks_provided: int
    tasks_consumed: int
    utilization: float          # abs(balance) / abs(hard_limit)


@dataclass(frozen=True)
class SettlementOutput:
    """Output of the settlement engine."""

    action: str                 # "settle" | "skip"
    peer_key: str
    amount: float               # settlement amount (positive = they pay us)
    reason: str                 # human-readable explanation
    target_utilization: float   # what utilization we're settling toward


def evaluate_settlement(inp: SettlementInput, config: dict) -> SettlementOutput:
    """Pure function: determines whether to settle with a peer and for how much.

    Default logic:
    - If utilization > soft_threshold (0.8): settle
    - Settlement amount brings utilization to soft_target (0.5)
    - target_balance = soft_limit - (soft_target * credit_limit)
    - amount = target_balance - balance
    - Skip if amount < min_settlement_amount (10.0)

    NaN/Inf rejection: all numeric inputs are validated. Non-finite values
    result in "skip" action with reason "INVALID_INPUT".

    Args:
        inp: SettlementInput with peer state
        config: Settlement config dict with soft_threshold, soft_target, min_settlement_amount

    Returns:
        SettlementOutput with action ("settle" or "skip") and amount
    """
    # Extract config with defaults
    soft_threshold = float(config.get("soft_threshold", 0.8))
    soft_target = float(config.get("soft_target", 0.5))
    min_settlement_amount = float(config.get("min_settlement_amount", 10.0))

    # Validate config values
    if not (math.isfinite(soft_threshold) and 0.0 <= soft_threshold <= 1.0):
        logger.warning(f"SETTLEMENT_ENGINE_INVALID_CONFIG soft_threshold={soft_threshold}")
        soft_threshold = 0.8

    if not (math.isfinite(soft_target) and 0.0 <= soft_target <= 1.0):
        logger.warning(f"SETTLEMENT_ENGINE_INVALID_CONFIG soft_target={soft_target}")
        soft_target = 0.5

    if not (math.isfinite(min_settlement_amount) and min_settlement_amount >= 0):
        logger.warning(f"SETTLEMENT_ENGINE_INVALID_CONFIG min_settlement_amount={min_settlement_amount}")
        min_settlement_amount = 10.0

    # NaN/Inf rejection on ALL numeric inputs
    for field_name, field_value in [
        ("balance", inp.balance),
        ("prepaid", inp.prepaid),
        ("pub_tab", inp.pub_tab),
        ("soft_limit", inp.soft_limit),
        ("hard_limit", inp.hard_limit),
        ("credit_limit", inp.credit_limit),
        ("utilization", inp.utilization),
    ]:
        if not math.isfinite(field_value):
            logger.warning(
                f"SETTLEMENT_ENGINE_INVALID_INPUT peer={inp.peer_key[:16]} "
                f"{field_name}={field_value!r}"
            )
            return SettlementOutput(
                action="skip",
                peer_key=inp.peer_key,
                amount=0.0,
                reason=f"INVALID_INPUT: {field_name} is not finite",
                target_utilization=soft_target,
            )

    # Skip if credit_limit is zero or negative (no credit relationship)
    if inp.credit_limit <= 0:
        return SettlementOutput(
            action="skip",
            peer_key=inp.peer_key,
            amount=0.0,
            reason="NO_CREDIT_LIMIT",
            target_utilization=soft_target,
        )

    # Check utilization threshold
    utilization = abs(inp.utilization)

    if utilization < soft_threshold:
        return SettlementOutput(
            action="skip",
            peer_key=inp.peer_key,
            amount=0.0,
            reason=f"Utilization {utilization:.2%} below threshold {soft_threshold:.2%}",
            target_utilization=soft_target,
        )

    # Calculate settlement amount: target at soft_target through full credit range
    # Matches netting.py: target = ic - soft_target * (ic - mb), settle = target - balance
    target_balance = inp.soft_limit - (soft_target * inp.credit_limit)
    amount = target_balance - inp.balance

    # Check minimum settlement amount (use abs — direction doesn't matter for minimum)
    if abs(amount) < min_settlement_amount:
        return SettlementOutput(
            action="skip",
            peer_key=inp.peer_key,
            amount=0.0,
            reason=f"Amount {amount:.2f} below minimum {min_settlement_amount:.2f}",
            target_utilization=soft_target,
        )

    # Settle!
    logger.info(
        f"SETTLEMENT_ENGINE_DECIDE peer={inp.peer_key[:16]} "
        f"utilization={utilization:.2%} amount={amount:.2f} "
        f"target_util={soft_target:.2%}"
    )

    return SettlementOutput(
        action="settle",
        peer_key=inp.peer_key,
        amount=round(amount, 6),
        reason=f"Utilization {utilization:.2%} exceeds threshold {soft_threshold:.2%}",
        target_utilization=soft_target,
    )
