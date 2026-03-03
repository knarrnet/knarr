"""Tests for admission gate module (P-010 pattern)."""

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from knarr.commerce.admission_gate import AdmissionRequest, AdmissionDecision, check_admission


CALLER = "a" * 64


class TestFreePass:
    def test_tit_for_tat_always_passes(self):
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="echo",
            base_price=100.0, balance=-999.0,
            soft_limit=-5.0, hard_limit=-10.0,
            tit_for_tat=True,
        )
        decision = check_admission(req)
        assert decision.outcome == "free_pass"
        assert decision.effective_price == 100.0

    def test_zero_price_always_passes(self):
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="ping",
            base_price=0.0, balance=-999.0,
            soft_limit=-5.0, hard_limit=-10.0,
        )
        decision = check_admission(req)
        assert decision.outcome == "free_pass"

    def test_negative_price_passes(self):
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="reward",
            base_price=-1.0, balance=0.0,
        )
        decision = check_admission(req)
        assert decision.outcome == "free_pass"


class TestAccepted:
    def test_healthy_balance(self):
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="llm-chat",
            base_price=1.0, balance=10.0,
            soft_limit=-5.0, hard_limit=-10.0,
        )
        decision = check_admission(req)
        assert decision.outcome == "accepted"
        assert decision.balance_after == 9.0

    def test_balance_above_soft_limit(self):
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="llm-chat",
            base_price=1.0, balance=-3.0,
            soft_limit=-5.0, hard_limit=-10.0,
        )
        decision = check_admission(req)
        assert decision.outcome == "accepted"
        assert decision.balance_after == -4.0

    def test_balance_exactly_at_soft_limit(self):
        """At soft limit boundary = accepted (not warning)."""
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="llm-chat",
            base_price=1.0, balance=-4.0,
            soft_limit=-5.0, hard_limit=-10.0,
        )
        decision = check_admission(req)
        assert decision.outcome == "accepted"
        assert decision.balance_after == -5.0


class TestSoftWarning:
    def test_crosses_soft_limit(self):
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="llm-chat",
            base_price=2.0, balance=-4.0,
            soft_limit=-5.0, hard_limit=-10.0,
        )
        decision = check_admission(req)
        assert decision.outcome == "soft_warning"
        assert decision.balance_after == -6.0
        assert decision.reason is not None

    def test_already_below_soft_limit(self):
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="llm-chat",
            base_price=1.0, balance=-7.0,
            soft_limit=-5.0, hard_limit=-10.0,
        )
        decision = check_admission(req)
        assert decision.outcome == "soft_warning"

    def test_just_below_soft_limit(self):
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="llm-chat",
            base_price=0.01, balance=-5.0,
            soft_limit=-5.0, hard_limit=-10.0,
        )
        decision = check_admission(req)
        assert decision.outcome == "soft_warning"
        assert decision.balance_after == pytest.approx(-5.01)


class TestHardBlock:
    def test_crosses_hard_limit(self):
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="llm-chat",
            base_price=5.0, balance=-6.0,
            soft_limit=-5.0, hard_limit=-10.0,
        )
        decision = check_admission(req)
        assert decision.outcome == "hard_block"
        assert decision.balance_after == -11.0
        assert "Insufficient credit" in decision.reason

    def test_already_below_hard_limit(self):
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="llm-chat",
            base_price=1.0, balance=-10.0,
            soft_limit=-5.0, hard_limit=-10.0,
        )
        decision = check_admission(req)
        assert decision.outcome == "hard_block"

    def test_exactly_at_hard_limit_boundary(self):
        """Balance after = hard_limit exactly = accepted (not blocked)."""
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="llm-chat",
            base_price=5.0, balance=-5.0,
            soft_limit=-5.0, hard_limit=-10.0,
        )
        decision = check_admission(req)
        # -5.0 - 5.0 = -10.0 which is NOT < -10.0, so soft_warning
        assert decision.outcome == "soft_warning"

    def test_large_price_exceeds_hard_limit(self):
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="expensive",
            base_price=100.0, balance=0.0,
            soft_limit=-5.0, hard_limit=-10.0,
        )
        decision = check_admission(req)
        assert decision.outcome == "hard_block"
        assert decision.balance_after == -100.0


class TestCustomLimits:
    def test_per_peer_soft_limit(self):
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="llm-chat",
            base_price=1.0, balance=0.0,
            soft_limit=-0.5, hard_limit=-1.0,
        )
        decision = check_admission(req)
        assert decision.outcome == "soft_warning"

    def test_generous_limits(self):
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="llm-chat",
            base_price=50.0, balance=0.0,
            soft_limit=-100.0, hard_limit=-200.0,
        )
        decision = check_admission(req)
        assert decision.outcome == "accepted"

    def test_zero_tolerance(self):
        """Hard limit = 0: any debit blocks."""
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="llm-chat",
            base_price=1.0, balance=0.0,
            soft_limit=0.0, hard_limit=0.0,
        )
        decision = check_admission(req)
        assert decision.outcome == "hard_block"


class TestDecisionDataclass:
    def test_frozen(self):
        decision = AdmissionDecision(outcome="accepted", effective_price=1.0)
        with pytest.raises(AttributeError):
            decision.outcome = "blocked"

    def test_request_frozen(self):
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="test",
            base_price=1.0, balance=0.0,
        )
        with pytest.raises(AttributeError):
            req.base_price = 999.0

    def test_effective_price_preserved(self):
        req = AdmissionRequest(
            caller_key=CALLER, skill_name="test",
            base_price=42.5, balance=100.0,
        )
        decision = check_admission(req)
        assert decision.effective_price == 42.5
