"""
Admission gate module — P-010 pattern (dataclass in, dataclass out).

Economic admission control: takes ledger state + price, returns decision.
Transport concerns (replay guard, version gate, skill resolution, visibility)
stay in the handler — this module is purely economic.

Wiring in node.py (~15 LOC):
    from ..commerce.admission_gate import AdmissionRequest, check_admission
    ...
    decision = check_admission(AdmissionRequest(
        caller_key=msg.public_key,
        skill_name=skill_name,
        base_price=skill_price,
        balance=entry.balance,
        soft_limit=soft_limit,
        hard_limit=hard_limit,
        tit_for_tat=self.policy.tit_for_tat,
    ))
    if decision.outcome == "hard_block":
        return self._sign(TaskResult(task_id=msg.task_id, status="failed",
            error={"code": "INSUFFICIENT_CREDIT", "message": decision.reason}))
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdmissionRequest:
    """Input to the admission gate."""

    caller_key: str
    skill_name: str
    base_price: float
    balance: float
    soft_limit: float = -5.0
    hard_limit: float = -10.0
    tit_for_tat: bool = False
    meter_count: int = 0  # F1 - Current meter count for rate limiting
    meter_max_count: int = 0  # F1 - Max allowed meter count (0 = no limit)


@dataclass(frozen=True)
class AdmissionDecision:
    """Output of the admission gate.

    Outcomes:
        "accepted"     — proceed to execution
        "soft_warning" — proceed but send tab reminder
        "hard_block"   — reject task
        "free_pass"    — tit-for-tat or zero-price, no balance check
    """

    outcome: str
    effective_price: float
    reason: Optional[str] = None
    balance_after: Optional[float] = None


def check_admission(req: AdmissionRequest) -> AdmissionDecision:
    """Evaluate admission for a single task request.

    Pure function: no side effects, no I/O. The caller handles
    bus events, receipts, and response construction.
    """
    # F1: Rate limit check - if meter_max_count > 0 and meter_count >= meter_max_count, hard block
    if req.meter_max_count > 0 and req.meter_count >= req.meter_max_count:
        logger.debug(
            f"ADMISSION_RATE_LIMIT caller={req.caller_key[:16]} "
            f"skill={req.skill_name} meter_count={req.meter_count} "
            f"max_count={req.meter_max_count}"
        )
        return AdmissionDecision(
            outcome="hard_block",
            effective_price=req.base_price,
            reason=(
                f"Rate limit exceeded: {req.meter_count} >= {req.meter_max_count} "
                f"executions in current window"
            ),
            balance_after=req.balance,
        )

    # Reject non-finite prices (NaN/Inf bypass all comparisons)
    if not math.isfinite(req.base_price):
        logger.warning(
            f"ADMISSION_INVALID_PRICE caller={req.caller_key[:16]} "
            f"skill={req.skill_name} base_price={req.base_price!r}"
        )
        return AdmissionDecision(
            outcome="hard_block",
            effective_price=0.0,
            reason=f"Invalid price: {req.base_price!r}",
            balance_after=req.balance,
        )

    # Free pass: tit-for-tat mode or zero-price skill
    if req.tit_for_tat or req.base_price <= 0:
        return AdmissionDecision(
            outcome="free_pass",
            effective_price=req.base_price,
            balance_after=req.balance,
        )

    # Project balance after this task
    balance_after = req.balance - req.base_price

    # Hard block: balance would drop below hard limit
    if balance_after < req.hard_limit:
        logger.debug(
            f"ADMISSION_HARD_BLOCK caller={req.caller_key[:16]} "
            f"skill={req.skill_name} balance={req.balance:.2f} "
            f"price={req.base_price:.2f} hard_limit={req.hard_limit:.2f}"
        )
        return AdmissionDecision(
            outcome="hard_block",
            effective_price=req.base_price,
            reason=(
                f"Insufficient credit: balance {req.balance:.2f} "
                f"- price {req.base_price:.2f} = {balance_after:.2f} "
                f"< hard limit {req.hard_limit:.2f}"
            ),
            balance_after=balance_after,
        )

    # Soft warning: balance would drop below soft limit
    if balance_after < req.soft_limit:
        logger.debug(
            f"ADMISSION_SOFT_WARNING caller={req.caller_key[:16]} "
            f"skill={req.skill_name} balance={req.balance:.2f} "
            f"price={req.base_price:.2f} soft_limit={req.soft_limit:.2f}"
        )
        return AdmissionDecision(
            outcome="soft_warning",
            effective_price=req.base_price,
            reason=(
                f"Credit warning: balance {req.balance:.2f} "
                f"approaching limit (soft={req.soft_limit:.2f})"
            ),
            balance_after=balance_after,
        )

    # Accepted: balance is healthy
    return AdmissionDecision(
        outcome="accepted",
        effective_price=req.base_price,
        balance_after=balance_after,
    )
