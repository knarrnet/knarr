"""Tests for prepaid_engine.py — B7 prepaid deduction (P-010 pattern)."""

import math
import pytest
from knarr.commerce.prepaid_engine import PrepaidRequest, PrepaidResult, evaluate_prepaid


def _make_req(
    peer_key="aabbcc",
    skill_name="test-skill",
    price=1.0,
    prepaid_balance=10.0,
    deduction_timing="at_execution",
):
    return PrepaidRequest(
        peer_key=peer_key,
        skill_name=skill_name,
        price=price,
        prepaid_balance=prepaid_balance,
        deduction_timing=deduction_timing,
    )


class TestPrepaidEngineDataclasses:
    def test_request_is_frozen(self):
        req = _make_req()
        with pytest.raises((AttributeError, TypeError)):
            req.price = 999.0

    def test_result_is_frozen(self):
        result = PrepaidResult(action="skip", amount=0.0, remaining=0.0, reason="test")
        with pytest.raises((AttributeError, TypeError)):
            result.action = "deduct"


class TestPrepaidEngineNanInfRejection:
    @pytest.mark.parametrize("bad_val", [float("nan"), float("inf"), float("-inf")])
    def test_nan_inf_price_rejected(self, bad_val):
        req = _make_req(price=bad_val, prepaid_balance=10.0)
        result = evaluate_prepaid(req)
        assert result.action == "reject"
        assert "Invalid price" in result.reason

    @pytest.mark.parametrize("bad_val", [float("nan"), float("inf")])
    def test_nan_inf_balance_rejected(self, bad_val):
        req = _make_req(price=1.0, prepaid_balance=bad_val)
        result = evaluate_prepaid(req)
        assert result.action == "reject"
        assert "Invalid" in result.reason


class TestPrepaidEngineSkip:
    def test_zero_balance_skips(self):
        """Zero balance = not a prepaid peer → skip (not a rejection)."""
        req = _make_req(prepaid_balance=0.0, price=1.0)
        result = evaluate_prepaid(req)
        assert result.action == "skip"
        assert result.amount == 0.0

    def test_zero_price_skips(self):
        """Free skill → skip deduction."""
        req = _make_req(price=0.0, prepaid_balance=5.0)
        result = evaluate_prepaid(req)
        assert result.action == "skip"

    def test_negative_price_skips(self):
        req = _make_req(price=-1.0, prepaid_balance=5.0)
        result = evaluate_prepaid(req)
        assert result.action == "skip"


class TestPrepaidEngineDeduct:
    def test_sufficient_balance_deducts(self):
        req = _make_req(price=3.0, prepaid_balance=10.0)
        result = evaluate_prepaid(req)
        assert result.action == "deduct"
        assert result.amount == pytest.approx(3.0)
        assert result.remaining == pytest.approx(7.0)

    def test_deduct_exact_balance(self):
        req = _make_req(price=10.0, prepaid_balance=10.0)
        result = evaluate_prepaid(req)
        assert result.action == "deduct"
        assert result.amount == pytest.approx(10.0)
        assert result.remaining == pytest.approx(0.0)


class TestPrepaidEngineInsufficient:
    def test_insufficient_balance_rejects_by_default(self):
        req = _make_req(price=15.0, prepaid_balance=10.0)
        result = evaluate_prepaid(req)
        assert result.action == "reject"
        assert "Insufficient" in result.reason
        assert result.amount == 0.0
        assert result.remaining == pytest.approx(10.0)  # unchanged

    def test_insufficient_with_partial_allowed_deducts(self):
        req = _make_req(price=15.0, prepaid_balance=10.0)
        config = {"allow_partial": True}
        result = evaluate_prepaid(req, config)
        assert result.action == "deduct"
        assert result.amount == pytest.approx(10.0)  # deduct what's available
        assert result.remaining == pytest.approx(0.0)


class TestPrepaidEnginePureFunction:
    def test_same_input_same_output(self):
        req = _make_req(price=2.0, prepaid_balance=8.0)
        r1 = evaluate_prepaid(req)
        r2 = evaluate_prepaid(req)
        assert r1.action == r2.action
        assert r1.amount == r2.amount
        assert r1.remaining == r2.remaining

    def test_no_side_effects(self):
        """evaluate_prepaid should not mutate its input."""
        req = _make_req(price=2.0, prepaid_balance=8.0)
        price_before = req.price
        balance_before = req.prepaid_balance
        evaluate_prepaid(req)
        assert req.price == price_before
        assert req.prepaid_balance == balance_before
