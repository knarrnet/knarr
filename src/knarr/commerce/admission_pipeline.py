"""
Admission pipeline — wires admission_gate + pricing_engine + documents.

B4 integration: Shows how the handler delegates to modules and produces
an admission_decision receipt. This replaces the inline credit check
in _handle_task_request().

Wiring in node.py (~20 LOC in handler):
    from ..commerce.admission_pipeline import run_admission, AdmissionContext

    ctx = AdmissionContext(
        caller_key=msg.public_key,
        skill_name=skill_name,
        base_price=skill.price,
        balance=entry.balance,
        soft_limit=entry.soft_limit or config_soft,
        hard_limit=entry.hard_limit or config_hard,
        tit_for_tat=self.policy.tit_for_tat,
        peer_node_id=caller_node_id,
        peer_groups=self._group_engine.get_groups(caller_node_id),
        discount_rules=self._load_discount_rules(caller_node_id, skill_name),
        cost_projection=self._get_cost_projection(skill_name),
        pricing_config=self._pricing_config,
    )
    result = run_admission(ctx)
    if result.gate.outcome == "hard_block":
        self._write_receipt(result.receipt, counterparty=caller_node_id, sign=True)
        return self._sign(TaskResult(status="failed", error=...))
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set

from .admission_gate import AdmissionDecision, AdmissionRequest, check_admission
from .documents import Document, admission_decision, price_calculation
from .pricing_engine import (
    DiscountRule,
    PricingConfig,
    PricingRequest,
    PricingResult,
    resolve_price,
)


@dataclass(frozen=True)
class AdmissionContext:
    """Everything needed to run a full admission check."""

    # Gate inputs
    caller_key: str
    skill_name: str
    base_price: float
    balance: float
    soft_limit: float = -5.0
    hard_limit: float = -10.0
    tit_for_tat: bool = False

    # Pricing inputs
    peer_node_id: str = ""
    peer_groups: Set[str] = field(default_factory=set)
    discount_rules: List[DiscountRule] = field(default_factory=list)
    cost_projection: Optional[float] = None
    skill_min_price: Optional[float] = None
    pricing_config: PricingConfig = field(default_factory=PricingConfig)

    # Receipt context
    identity: str = ""  # this node's ID
    counterparty: str = ""  # caller's node ID


@dataclass(frozen=True)
class AdmissionResult:
    """Complete result of admission pipeline."""

    gate: AdmissionDecision
    pricing: PricingResult
    receipt: Document  # admission_decision receipt


def run_admission(ctx: AdmissionContext) -> AdmissionResult:
    """Run the full admission pipeline: price → gate → receipt.

    Pure function: no side effects. Caller handles bus events,
    storage writes, and response construction.
    """
    # Step 1: Resolve effective price
    pricing = resolve_price(
        PricingRequest(
            base_price=ctx.base_price,
            skill_name=ctx.skill_name,
            peer_node_id=ctx.peer_node_id,
            peer_groups=ctx.peer_groups,
            discount_rules=ctx.discount_rules,
            cost_projection=ctx.cost_projection,
            skill_min_price=ctx.skill_min_price,
        ),
        ctx.pricing_config,
    )

    # Step 2: Gate check with effective price
    gate = check_admission(
        AdmissionRequest(
            caller_key=ctx.caller_key,
            skill_name=ctx.skill_name,
            base_price=pricing.final_price,
            balance=ctx.balance,
            soft_limit=ctx.soft_limit,
            hard_limit=ctx.hard_limit,
            tit_for_tat=ctx.tit_for_tat,
        )
    )

    # Step 3: Build admission decision receipt
    receipt = admission_decision(
        skill_name=ctx.skill_name,
        decision={
            "outcome": gate.outcome,
            "effective_price": pricing.final_price,
            "balance_before": ctx.balance,
            "balance_after": gate.balance_after,
        },
        identity=ctx.identity,
        counterparty=ctx.counterparty,
        pricing={
            "base_price": pricing.base_price,
            "final_price": pricing.final_price,
            "discount_mode": pricing.discount_mode,
            "rules_applied": pricing.rules_applied,
            "floor_applied": pricing.floor_applied,
            "cap_applied": pricing.cap_applied,
        },
    )

    return AdmissionResult(gate=gate, pricing=pricing, receipt=receipt)
