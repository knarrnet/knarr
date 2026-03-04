"""
Prepaid deduction engine — P-010 pattern (dataclass in, dataclass out).

Determines whether to deduct from a peer's prepaid balance before execution.
Pure function, no side effects, no I/O.

Config keys (under [economy.prepaid]):
    engine       = "default"        # importlib-loadable module
    timing       = "at_execution"   # "at_execution" | "at_request"
    allow_partial = false           # partial deductions allowed
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrepaidRequest:
    """Input to the prepaid deduction engine."""

    peer_key: str
    skill_name: str
    price: float                # from pricing engine
    prepaid_balance: float      # current prepaid balance for this peer
    deduction_timing: str = "at_execution"  # "at_execution" | "at_request"


@dataclass(frozen=True)
class PrepaidResult:
    """Output of the prepaid deduction engine.

    Actions:
        "deduct"  — deduct amount from prepaid balance
        "reject"  — reject task (insufficient balance)
        "skip"    — peer is not prepaid (balance == 0); use normal billing
    """

    action: str           # "deduct" | "reject" | "skip"
    amount: float         # amount to deduct (0 on skip/reject)
    remaining: float      # balance after deduction (unchanged on skip/reject)
    reason: str


def evaluate_prepaid(req: PrepaidRequest, config: dict = None) -> PrepaidResult:
    """Determine whether to deduct from prepaid balance.

    Pure function: no side effects, no I/O, no SQL.

    Default logic:
    - If prepaid_balance == 0: skip (not a prepaid peer)
    - If price is non-finite: reject (invalid price)
    - If prepaid_balance < price and allow_partial is False: reject
    - Otherwise: deduct full price at execution time

    Config keys (under [economy.prepaid]):
        allow_partial  — allow partial deductions (default: False)
        timing         — "at_execution" (default) | "at_request"
    """
    if config is None:
        config = {}

    allow_partial = bool(config.get("allow_partial", False))

    # NaN/Inf rejection on numeric inputs
    if not math.isfinite(req.price):
        logger.warning(
            f"PREPAID_INVALID_PRICE peer={req.peer_key[:16]} "
            f"skill={req.skill_name} price={req.price!r}"
        )
        return PrepaidResult(
            action="reject",
            amount=0.0,
            remaining=req.prepaid_balance,
            reason=f"Invalid price: {req.price!r}",
        )

    if not math.isfinite(req.prepaid_balance):
        logger.warning(
            f"PREPAID_INVALID_BALANCE peer={req.peer_key[:16]} "
            f"balance={req.prepaid_balance!r}"
        )
        return PrepaidResult(
            action="reject",
            amount=0.0,
            remaining=0.0,
            reason=f"Invalid prepaid balance: {req.prepaid_balance!r}",
        )

    # Skip: peer has zero prepaid balance — use normal credit billing
    if req.prepaid_balance == 0.0:
        logger.debug(
            f"PREPAID_SKIP peer={req.peer_key[:16]} "
            f"skill={req.skill_name} — not a prepaid peer"
        )
        return PrepaidResult(
            action="skip",
            amount=0.0,
            remaining=0.0,
            reason="Not a prepaid peer (zero balance)",
        )

    # Zero price: skip deduction (free skill)
    if req.price <= 0.0:
        return PrepaidResult(
            action="skip",
            amount=0.0,
            remaining=req.prepaid_balance,
            reason="Free skill — no deduction required",
        )

    # Insufficient balance
    if req.prepaid_balance < req.price:
        if allow_partial:
            # Deduct whatever is available
            deduct = req.prepaid_balance
            remaining = 0.0
            logger.debug(
                f"PREPAID_PARTIAL peer={req.peer_key[:16]} "
                f"skill={req.skill_name} deduct={deduct:.4f} "
                f"of price={req.price:.4f}"
            )
            return PrepaidResult(
                action="deduct",
                amount=round(deduct, 6),
                remaining=round(remaining, 6),
                reason=(
                    f"Partial deduction: balance {req.prepaid_balance:.4f} "
                    f"< price {req.price:.4f} — partial allowed"
                ),
            )
        else:
            logger.debug(
                f"PREPAID_INSUFFICIENT peer={req.peer_key[:16]} "
                f"skill={req.skill_name} balance={req.prepaid_balance:.4f} "
                f"price={req.price:.4f}"
            )
            return PrepaidResult(
                action="reject",
                amount=0.0,
                remaining=req.prepaid_balance,
                reason=(
                    f"Insufficient prepaid balance: "
                    f"{req.prepaid_balance:.4f} < {req.price:.4f}"
                ),
            )

    # Full deduction
    remaining = req.prepaid_balance - req.price
    logger.debug(
        f"PREPAID_DEDUCT peer={req.peer_key[:16]} "
        f"skill={req.skill_name} amount={req.price:.4f} "
        f"remaining={remaining:.4f}"
    )
    return PrepaidResult(
        action="deduct",
        amount=round(req.price, 6),
        remaining=round(remaining, 6),
        reason=f"Full deduction at execution: {req.price:.4f}",
    )
