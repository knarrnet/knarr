"""Tests for pricing engine module (P-010 pattern)."""

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from knarr.commerce.pricing_engine import (
    DiscountRule,
    PricingConfig,
    PricingRequest,
    PricingResult,
    resolve_price,
)


def _rule(name="test", group="felag_a", effect=10.0, priority=0, skill="*"):
    return DiscountRule(
        name=name, group_name=group, skill_group=skill,
        effect_pct=effect, priority=priority,
    )


class TestNoDiscounts:
    def test_base_price_passthrough(self):
        req = PricingRequest(base_price=10.0, skill_name="llm-chat", peer_node_id="a" * 64)
        result = resolve_price(req, PricingConfig())
        assert result.final_price == 10.0
        assert result.floor_applied is False
        assert result.cap_applied is False
        assert result.rules_applied == []

    def test_zero_price(self):
        """Zero-price skill needs min_price=0 to stay free."""
        req = PricingRequest(base_price=0.0, skill_name="ping", peer_node_id="a" * 64)
        result = resolve_price(req, PricingConfig(min_price=0.0))
        assert result.final_price == 0.0

    def test_zero_price_stays_free(self):
        """v0.39.0: base_price=0.0 is intentional free skill — bypasses floor."""
        req = PricingRequest(base_price=0.0, skill_name="ping", peer_node_id="a" * 64)
        result = resolve_price(req, PricingConfig())
        assert result.final_price == 0.0
        assert result.floor_applied is False


class TestMultiplicativeMode:
    def test_single_discount(self):
        req = PricingRequest(
            base_price=100.0, skill_name="llm-chat",
            peer_node_id="a" * 64,
            discount_rules=[_rule(effect=10.0)],
        )
        result = resolve_price(req, PricingConfig(discount_mode="multiplicative"))
        assert result.final_price == 90.0

    def test_two_discounts_compound(self):
        """10% + 10% multiplicative = 19% total (not 20%)."""
        req = PricingRequest(
            base_price=100.0, skill_name="llm-chat",
            peer_node_id="a" * 64,
            discount_rules=[_rule(name="a", effect=10.0), _rule(name="b", effect=10.0)],
        )
        result = resolve_price(req, PricingConfig(discount_mode="multiplicative"))
        assert result.final_price == 81.0
        assert len(result.rules_applied) == 2

    def test_three_discounts(self):
        req = PricingRequest(
            base_price=100.0, skill_name="s",
            peer_node_id="a" * 64,
            discount_rules=[
                _rule(name="a", effect=10.0, priority=2),
                _rule(name="b", effect=20.0, priority=1),
                _rule(name="c", effect=5.0, priority=0),
            ],
        )
        result = resolve_price(req, PricingConfig(discount_mode="multiplicative"))
        # 100 * 0.9 * 0.8 * 0.95 = 68.4
        assert result.final_price == 68.4

    def test_priority_ordering(self):
        """Higher priority rules applied first."""
        req = PricingRequest(
            base_price=100.0, skill_name="s",
            peer_node_id="a" * 64,
            discount_rules=[
                _rule(name="low", effect=10.0, priority=0),
                _rule(name="high", effect=20.0, priority=10),
            ],
        )
        result = resolve_price(req, PricingConfig(discount_mode="multiplicative"))
        assert result.rules_applied[0]["name"] == "high"
        assert result.rules_applied[1]["name"] == "low"


class TestAdditiveMode:
    def test_single_discount(self):
        req = PricingRequest(
            base_price=100.0, skill_name="s",
            peer_node_id="a" * 64,
            discount_rules=[_rule(effect=10.0)],
        )
        result = resolve_price(req, PricingConfig(discount_mode="additive"))
        assert result.final_price == 90.0

    def test_two_discounts_additive(self):
        """10% + 10% additive = 20% total."""
        req = PricingRequest(
            base_price=100.0, skill_name="s",
            peer_node_id="a" * 64,
            discount_rules=[_rule(name="a", effect=10.0), _rule(name="b", effect=10.0)],
        )
        result = resolve_price(req, PricingConfig(discount_mode="additive"))
        assert result.final_price == 80.0

    def test_capped_at_100_percent(self):
        """Total discount can't exceed 100%."""
        req = PricingRequest(
            base_price=100.0, skill_name="s",
            peer_node_id="a" * 64,
            discount_rules=[_rule(name="a", effect=60.0), _rule(name="b", effect=60.0)],
        )
        result = resolve_price(req, PricingConfig(discount_mode="additive", min_price=0.0))
        assert result.final_price == 0.0


class TestBestWinsMode:
    def test_takes_largest_discount(self):
        req = PricingRequest(
            base_price=100.0, skill_name="s",
            peer_node_id="a" * 64,
            discount_rules=[
                _rule(name="small", effect=5.0),
                _rule(name="big", effect=30.0),
                _rule(name="medium", effect=15.0),
            ],
        )
        result = resolve_price(req, PricingConfig(discount_mode="best_wins"))
        assert result.final_price == 70.0
        assert len(result.rules_applied) == 1
        assert result.rules_applied[0]["name"] == "big"


class TestDiscountCap:
    def test_cap_limits_discount(self):
        req = PricingRequest(
            base_price=100.0, skill_name="s",
            peer_node_id="a" * 64,
            discount_rules=[_rule(effect=50.0)],
        )
        config = PricingConfig(discount_cap_pct=30.0)
        result = resolve_price(req, config)
        assert result.final_price == 70.0
        assert result.cap_applied is True

    def test_cap_not_exceeded(self):
        req = PricingRequest(
            base_price=100.0, skill_name="s",
            peer_node_id="a" * 64,
            discount_rules=[_rule(effect=10.0)],
        )
        config = PricingConfig(discount_cap_pct=30.0)
        result = resolve_price(req, config)
        assert result.final_price == 90.0
        assert result.cap_applied is False


class TestFloor:
    def test_cost_projection_floor(self):
        """Price can't go below cost * markup."""
        req = PricingRequest(
            base_price=100.0, skill_name="s",
            peer_node_id="a" * 64,
            discount_rules=[_rule(effect=95.0)],
            cost_projection=10.0,
        )
        config = PricingConfig(markup_minimum=1.1, min_price=0.01, discount_cap_pct=100.0)
        result = resolve_price(req, config)
        # 95% off = $5, but cost floor = 10 * 1.1 = $11
        assert result.final_price == 11.0
        assert result.floor_applied is True
        assert result.floor_price == 11.0

    def test_min_price_floor(self):
        req = PricingRequest(
            base_price=1.0, skill_name="s",
            peer_node_id="a" * 64,
            discount_rules=[_rule(effect=99.0)],
        )
        config = PricingConfig(min_price=0.5, discount_cap_pct=100.0)
        result = resolve_price(req, config)
        assert result.final_price == 0.5
        assert result.floor_applied is True

    def test_per_skill_min_price(self):
        req = PricingRequest(
            base_price=10.0, skill_name="premium",
            peer_node_id="a" * 64,
            discount_rules=[_rule(effect=99.0)],
            skill_min_price=5.0,
        )
        config = PricingConfig(min_price=0.01, discount_cap_pct=100.0)
        result = resolve_price(req, config)
        assert result.final_price == 5.0
        assert result.floor_applied is True

    def test_global_min_price(self):
        req = PricingRequest(
            base_price=0.5, skill_name="cheap",
            peer_node_id="a" * 64,
        )
        config = PricingConfig(global_min_price=1.0)
        result = resolve_price(req, config)
        assert result.final_price == 1.0
        assert result.floor_applied is True

    def test_no_floor_when_price_is_above(self):
        req = PricingRequest(
            base_price=100.0, skill_name="s",
            peer_node_id="a" * 64,
            cost_projection=5.0,
        )
        config = PricingConfig(markup_minimum=1.1, min_price=0.01)
        result = resolve_price(req, config)
        assert result.final_price == 100.0
        assert result.floor_applied is False


class TestBreakdown:
    def test_breakdown_fields_present(self):
        req = PricingRequest(
            base_price=100.0, skill_name="s",
            peer_node_id="a" * 64,
            discount_rules=[_rule(effect=10.0)],
            cost_projection=5.0,
        )
        result = resolve_price(req, PricingConfig())
        assert result.base_price == 100.0
        assert result.cost_projection == 5.0
        assert result.discount_mode == "multiplicative"
        assert len(result.rules_applied) == 1

    def test_result_is_frozen(self):
        req = PricingRequest(base_price=10.0, skill_name="s", peer_node_id="a" * 64)
        result = resolve_price(req, PricingConfig())
        with pytest.raises(AttributeError):
            result.final_price = 999.0


class TestEdgeCases:
    def test_100_percent_discount_hits_floor(self):
        req = PricingRequest(
            base_price=100.0, skill_name="s",
            peer_node_id="a" * 64,
            discount_rules=[_rule(effect=100.0)],
        )
        config = PricingConfig(min_price=0.01, discount_cap_pct=100.0)
        result = resolve_price(req, config)
        assert result.final_price == 0.01
        assert result.floor_applied is True

    def test_rounding_to_6_decimals(self):
        req = PricingRequest(
            base_price=100.0, skill_name="s",
            peer_node_id="a" * 64,
            discount_rules=[_rule(effect=33.333333)],
        )
        result = resolve_price(req, PricingConfig(min_price=0.0))
        # 100 * (1 - 0.33333333) = 66.666667
        assert result.final_price == pytest.approx(66.666667, abs=0.000001)
        # Exactly 6 decimals
        as_str = f"{result.final_price:.6f}"
        assert len(as_str.split(".")[1]) == 6

    def test_unknown_mode_falls_back(self):
        req = PricingRequest(
            base_price=100.0, skill_name="s",
            peer_node_id="a" * 64,
            discount_rules=[_rule(effect=10.0)],
        )
        result = resolve_price(req, PricingConfig(discount_mode="invalid_mode"))
        # Falls back to multiplicative
        assert result.final_price == 90.0
