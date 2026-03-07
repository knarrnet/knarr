"""
Pricing engine module — P-010 pattern (dataclass in, dataclass out).

Computes final price from base price through discount rules, caps, and floors.
Standalone, testable, no node dependencies.

Wiring in node.py (~10 LOC):
    from ..commerce.pricing_engine import PricingRequest, PricingConfig, resolve_price

    config = PricingConfig(
        discount_mode=self._config.get("pricing", {}).get("discount_mode", "multiplicative"),
        ...
    )
    result = resolve_price(PricingRequest(
        base_price=skill.price,
        skill_name=skill_name,
        peer_node_id=caller_node_id,
        peer_groups=self._group_engine.get_groups(caller_node_id),
        discount_rules=self._load_discount_rules(caller_node_id, skill_name),
        cost_projection=self._get_cost_projection(skill_name),
    ), config)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscountRule:
    """A single discount rule (from SQL or TOML config)."""

    name: str
    group_name: str
    skill_group: str  # "*" for wildcard, or specific skill name
    effect_pct: float  # percentage discount (10.0 = 10% off)
    priority: int = 0


@dataclass(frozen=True)
class PricingConfig:
    """Engine-level configuration (from node config, not per-request)."""

    discount_mode: str = "multiplicative"  # "multiplicative", "additive", "best_wins"
    discount_cap_pct: float = 100.0  # max total discount percentage
    markup_minimum: float = 1.1  # cost * markup = dynamic floor
    min_price: float = 0.01  # absolute minimum price
    global_min_price: float = 0.0  # v0.33.0 global floor
    bounty_decay: float = 1.0  # F1 - Multiplicative decay factor for bounties (0.8 = 20% decay per execution)


@dataclass(frozen=True)
class PricingRequest:
    """Input to the pricing engine."""

    base_price: float
    skill_name: str
    peer_node_id: str
    peer_groups: Set[str] = field(default_factory=set)
    discount_rules: List[DiscountRule] = field(default_factory=list)
    cost_projection: Optional[float] = None
    skill_min_price: Optional[float] = None  # per-skill floor override
    meter_count: int = 0  # F1 - Used for bounty decay calculation


@dataclass(frozen=True)
class PricingResult:
    """Output of the pricing engine."""

    final_price: float
    base_price: float
    cost_projection: Optional[float]
    rules_applied: List[Dict]
    discount_mode: str
    floor_price: float
    floor_applied: bool
    cap_applied: bool


def resolve_price(req: PricingRequest, config: PricingConfig) -> PricingResult:
    """Compute final price with structured breakdown.

    Pure function: no side effects, no I/O, no SQL.
    The caller loads discount rules and cost projections before calling.
    """
    # Reject non-finite base_price (NaN/Inf bypass floor/cap logic)
    if not math.isfinite(req.base_price):
        logger.warning(f"PRICING_INVALID_BASE base_price={req.base_price!r} skill={req.skill_name}")
        return PricingResult(
            final_price=config.min_price, base_price=req.base_price,
            cost_projection=req.cost_projection, rules_applied=[],
            discount_mode=config.discount_mode, floor_price=config.min_price,
            floor_applied=True, cap_applied=False,
        )

    # Clamp discount_cap_pct to [0, 100]
    effective_cap_pct = min(max(config.discount_cap_pct, 0.0), 100.0)

    # F1: Apply bounty decay if skill is a bounty (negative price) and meter_count > 0
    base_price = req.base_price
    if req.base_price < 0 and req.meter_count > 0:
        # Clamp decay to (0, 1.0] — 0 would collapse bounty to 0, >1 inflates exponentially
        clamped_decay = min(max(config.bounty_decay, 1e-9), 1.0)
        decay_factor = clamped_decay ** req.meter_count
        base_price = min(req.base_price * decay_factor, 0.0)
        logger.debug(
            f"BOUNTY_DECAY skill={req.skill_name} original={req.base_price} "
            f"count={req.meter_count} decay={config.bounty_decay} "
            f"result={base_price}"
        )

    price = base_price
    rules_applied: List[Dict] = []

    if not req.discount_rules:
        # No discounts — skip to floor
        return _apply_floor(price, req, config, rules_applied, cap_applied=False)

    # Sort by priority descending (highest first)
    sorted_rules = sorted(req.discount_rules, key=lambda r: r.priority, reverse=True)

    if config.discount_mode == "multiplicative":
        for rule in sorted_rules:
            clamped_pct = min(max(rule.effect_pct, 0.0), 100.0)
            factor = 1.0 - clamped_pct / 100.0
            price *= factor
            rules_applied.append({
                "name": rule.name,
                "effect_pct": clamped_pct,
                "factor": round(factor, 6),
            })

    elif config.discount_mode == "additive":
        total_pct = sum(min(max(r.effect_pct, 0.0), 100.0) for r in sorted_rules)
        total_pct = min(total_pct, 100.0)  # cap at 100% discount
        price = base_price * (1.0 - total_pct / 100.0)
        for rule in sorted_rules:
            rules_applied.append({
                "name": rule.name,
                "effect_pct": min(max(rule.effect_pct, 0.0), 100.0),
            })

    elif config.discount_mode == "best_wins":
        best = max(sorted_rules, key=lambda r: min(max(r.effect_pct, 0.0), 100.0))
        clamped_pct = min(max(best.effect_pct, 0.0), 100.0)
        price = base_price * (1.0 - clamped_pct / 100.0)
        rules_applied.append({
            "name": best.name,
            "effect_pct": clamped_pct,
        })

    else:
        logger.warning(f"PRICING_UNKNOWN_MODE mode={config.discount_mode}, falling back to multiplicative")
        for rule in sorted_rules:
            clamped_pct = min(max(rule.effect_pct, 0.0), 100.0)
            factor = 1.0 - clamped_pct / 100.0
            price *= factor
            rules_applied.append({
                "name": rule.name,
                "effect_pct": clamped_pct,
                "factor": round(factor, 6),
            })

    # Apply discount cap
    cap_applied = False
    if base_price >= 0:
        max_discount = base_price * (effective_cap_pct / 100.0)
        actual_discount = base_price - price
        if actual_discount > max_discount:
            price = base_price - max_discount
            cap_applied = True

    return _apply_floor(price, req, config, rules_applied, cap_applied)


def _apply_floor(
    price: float,
    req: PricingRequest,
    config: PricingConfig,
    rules_applied: List[Dict],
    cap_applied: bool,
) -> PricingResult:
    """Apply price floor (cost projection + minimum price)."""
    floor_applied = False

    if price < 0:
        return PricingResult(
            final_price=round(min(price, 0.0), 6),
            base_price=req.base_price,
            cost_projection=req.cost_projection,
            rules_applied=rules_applied,
            discount_mode=config.discount_mode,
            floor_price=0.0,
            floor_applied=False,
            cap_applied=cap_applied,
        )

    # Dynamic floor from cost projection
    if req.cost_projection is not None and math.isfinite(req.cost_projection) and req.cost_projection > 0:
        dynamic_floor = req.cost_projection * config.markup_minimum
    else:
        dynamic_floor = 0.0

    # Per-skill floor
    skill_floor = req.skill_min_price if req.skill_min_price is not None else config.min_price

    # Floor = max(dynamic_floor, skill_floor)
    floor_price = max(dynamic_floor, skill_floor)

    if price < floor_price:
        price = floor_price
        floor_applied = True

    # Global minimum (v0.33.0)
    if config.global_min_price > 0 and price < config.global_min_price:
        price = config.global_min_price
        floor_applied = True
        floor_price = max(floor_price, config.global_min_price)

    final_price = round(max(price, 0.0), 6)

    return PricingResult(
        final_price=final_price,
        base_price=req.base_price,
        cost_projection=req.cost_projection,
        rules_applied=rules_applied,
        discount_mode=config.discount_mode,
        floor_price=round(floor_price, 6),
        floor_applied=floor_applied,
        cap_applied=cap_applied,
    )
