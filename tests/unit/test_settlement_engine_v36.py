"""Tests for settlement_engine.py — B1 pure function (P-010 pattern)."""

import math
import pytest
from knarr.commerce.settlement_engine import (
    SettlementInput,
    SettlementOutput,
    evaluate_settlement,
)


def _make_inp(
    peer_key="aabbcc",
    balance=-8.0,
    prepaid=0.0,
    pub_tab=0.0,
    soft_limit=0.0,
    hard_limit=-10.0,
    credit_limit=10.0,
    tasks_provided=5,
    tasks_consumed=3,
    utilization=0.8,
):
    return SettlementInput(
        peer_key=peer_key,
        balance=balance,
        prepaid=prepaid,
        pub_tab=pub_tab,
        soft_limit=soft_limit,
        hard_limit=hard_limit,
        credit_limit=credit_limit,
        tasks_provided=tasks_provided,
        tasks_consumed=tasks_consumed,
        utilization=utilization,
    )


def _default_config():
    return {
        "soft_threshold": 0.8,
        "soft_target": 0.5,
        "min_settlement_amount": 1.0,
    }


class TestSettlementEngineDataclasses:
    def test_input_is_frozen(self):
        inp = _make_inp()
        with pytest.raises((AttributeError, TypeError)):
            inp.balance = 999.0

    def test_output_is_frozen(self):
        out = SettlementOutput(
            action="skip", peer_key="abc", amount=0.0,
            reason="test", target_utilization=0.0,
        )
        with pytest.raises((AttributeError, TypeError)):
            out.action = "settle"

    def test_input_fields_accessible(self):
        inp = _make_inp(balance=-7.5, utilization=0.75)
        assert inp.balance == -7.5
        assert inp.utilization == 0.75
        assert inp.peer_key == "aabbcc"


class TestSettlementEngineNanInfRejection:
    """NaN/Inf must be rejected at the boundary — critical security property."""

    @pytest.mark.parametrize("bad_val", [float("nan"), float("inf"), float("-inf")])
    def test_nan_inf_balance_rejected(self, bad_val):
        inp = _make_inp(balance=bad_val)
        out = evaluate_settlement(inp, _default_config())
        assert out.action == "skip"
        assert "not finite" in out.reason.lower() or "non-finite" in out.reason.lower()

    @pytest.mark.parametrize("bad_val", [float("nan"), float("inf")])
    def test_nan_inf_utilization_rejected(self, bad_val):
        inp = _make_inp(utilization=bad_val)
        out = evaluate_settlement(inp, _default_config())
        assert out.action == "skip"

    @pytest.mark.parametrize("bad_val", [float("nan"), float("inf")])
    def test_nan_inf_credit_limit_rejected(self, bad_val):
        inp = _make_inp(credit_limit=bad_val)
        out = evaluate_settlement(inp, _default_config())
        assert out.action == "skip"

    def test_nan_in_config_rejected(self):
        inp = _make_inp(utilization=0.9)
        config = {"soft_threshold": float("nan"), "soft_target": 0.5, "min_settlement_amount": 10.0}
        out = evaluate_settlement(inp, config)
        assert out.action == "skip"


class TestSettlementEngineThreshold:
    def test_above_threshold_triggers_settle(self):
        # utilization=0.85 > threshold=0.8
        inp = _make_inp(balance=-8.5, utilization=0.85)
        out = evaluate_settlement(inp, _default_config())
        assert out.action == "settle"
        assert out.amount != 0.0

    def test_below_threshold_skips(self):
        # utilization=0.75 < threshold=0.8
        inp = _make_inp(balance=-7.5, utilization=0.75)
        out = evaluate_settlement(inp, _default_config())
        assert out.action == "skip"
        assert "below threshold" in out.reason.lower()

    def test_exactly_at_threshold_triggers(self):
        # utilization exactly 0.8 at threshold=0.8 — the condition is >= so this triggers
        inp = _make_inp(balance=-8.0, utilization=0.8)
        out = evaluate_settlement(inp, _default_config())
        # utilization >= soft_threshold triggers settlement (not strictly >)
        assert out.action == "settle"

    def test_custom_threshold(self):
        # Custom threshold=0.5: utilization=0.6 should trigger
        inp = _make_inp(utilization=0.6)
        config = {"soft_threshold": 0.5, "soft_target": 0.2, "min_settlement_amount": 1.0}
        out = evaluate_settlement(inp, config)
        assert out.action == "settle"


class TestSettlementEngineAmount:
    def test_settlement_amount_formula(self):
        # Spec formula: amount = balance - (soft_target * hard_limit)
        # balance=-8.0, hard_limit=-10.0, soft_target=0.5
        # amount = -8.0 - (0.5 * -10.0) = -8.0 + 5.0 = -3.0
        inp = _make_inp(
            balance=-8.0, hard_limit=-10.0, credit_limit=10.0, utilization=0.85
        )
        config = {"soft_threshold": 0.8, "soft_target": 0.5, "min_settlement_amount": 1.0}
        out = evaluate_settlement(inp, config)
        assert out.action == "settle"
        assert abs(out.amount - (-3.0)) < 0.01

    def test_min_settlement_amount_respected(self):
        # Small amount below min_settlement_amount should skip
        inp = _make_inp(
            balance=-8.1, hard_limit=-10.0, credit_limit=10.0, utilization=0.85,
        )
        config = {
            "soft_threshold": 0.8,
            "soft_target": 0.5,
            "min_settlement_amount": 100.0,  # very high min
        }
        out = evaluate_settlement(inp, config)
        assert out.action == "skip"
        assert "minimum" in out.reason.lower() or "min" in out.reason.lower()

    def test_zero_credit_range_skips(self):
        inp = _make_inp(credit_limit=0.0)
        out = evaluate_settlement(inp, _default_config())
        assert out.action == "skip"

    def test_target_utilization_in_output(self):
        inp = _make_inp(utilization=0.9)
        config = {"soft_threshold": 0.8, "soft_target": 0.5, "min_settlement_amount": 1.0}
        out = evaluate_settlement(inp, config)
        if out.action == "settle":
            assert out.target_utilization == pytest.approx(0.5)


class TestSettlementEngineDefaultConfig:
    def test_empty_config_uses_defaults(self):
        # With empty config, defaults: threshold=0.8, target=0.5, min=10
        # Spec formula: amount = balance - (soft_target * hard_limit)
        # balance=-20, hard_limit=-10: amount = -20 - (0.5 * -10) = -20 + 5 = -15, abs=15 >= min=10
        inp = _make_inp(utilization=0.9, balance=-20.0, credit_limit=20.0, hard_limit=-10.0)
        out = evaluate_settlement(inp, {})
        assert out.action == "settle"

    def test_pure_function_no_side_effects(self):
        """Calling evaluate_settlement multiple times with same input gives same output."""
        inp = _make_inp(utilization=0.9)
        config = {"soft_threshold": 0.8, "soft_target": 0.5, "min_settlement_amount": 1.0}
        out1 = evaluate_settlement(inp, config)
        out2 = evaluate_settlement(inp, config)
        assert out1.action == out2.action
        assert out1.amount == out2.amount

    def test_output_peer_key_matches_input(self):
        inp = _make_inp(peer_key="deadbeef", utilization=0.9)
        config = {"soft_threshold": 0.8, "soft_target": 0.5, "min_settlement_amount": 1.0}
        out = evaluate_settlement(inp, config)
        assert out.peer_key == "deadbeef"
