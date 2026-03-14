"""Tests for B3 economy summary + B4 admission pipeline integration."""

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from knarr.commerce.economy import (
    EconomySummary,
    PeerEconomy,
    get_economy_summary,
    peer_economy_from_row,
)
from knarr.commerce.admission_pipeline import (
    AdmissionContext,
    AdmissionResult,
    run_admission,
)
from knarr.commerce.pricing_engine import DiscountRule, PricingConfig


def _ledger_row(
    peer_key="aaa", balance=0.0, prepaid=0.0, pub_tab=0.0,
    soft_limit=-5.0, hard_limit=-10.0, credit_limit=3.0,
    tasks_provided=0, tasks_consumed=0, trust=0.3,
):
    return {
        "peer_public_key": peer_key,
        "balance": balance, "prepaid": prepaid, "pub_tab": pub_tab,
        "soft_limit": soft_limit, "hard_limit": hard_limit,
        "credit_limit": credit_limit,
        "tasks_provided": tasks_provided, "tasks_consumed": tasks_consumed,
        "trust": trust,
    }


# ── B3: Economy Summary ────────────────────────────────────────────────


class TestEconomySummary:
    def test_empty_ledger(self):
        summary = get_economy_summary([])
        assert summary.total_peers == 0
        assert summary.total_balance == 0.0
        assert summary.active_peers == 0

    def test_single_peer(self):
        summary = get_economy_summary([_ledger_row(balance=5.0, tasks_provided=10)])
        assert summary.total_peers == 1
        assert summary.total_balance == 5.0
        assert summary.active_peers == 1
        assert summary.total_tasks_provided == 10

    def test_multiple_peers(self):
        rows = [
            _ledger_row(peer_key="a", balance=10.0, tasks_provided=5, prepaid=2.0),
            _ledger_row(peer_key="b", balance=-3.0, tasks_consumed=8, pub_tab=1.0),
            _ledger_row(peer_key="c", balance=0.0),  # inactive
        ]
        summary = get_economy_summary(rows)
        assert summary.total_peers == 3
        assert summary.active_peers == 2  # a and b have non-zero balance
        assert summary.total_balance == 7.0
        assert summary.total_prepaid == 2.0
        assert summary.total_pub_tab == 1.0
        assert summary.total_tasks_provided == 5
        assert summary.total_tasks_consumed == 8

    def test_peers_at_limits(self):
        rows = [
            _ledger_row(peer_key="ok", balance=0.0),
            _ledger_row(peer_key="soft", balance=-6.0, soft_limit=-5.0),
            _ledger_row(peer_key="hard", balance=-11.0, soft_limit=-5.0, hard_limit=-10.0),
        ]
        summary = get_economy_summary(rows)
        assert summary.peers_at_soft_limit == 2  # soft and hard both below soft
        assert summary.peers_at_hard_limit == 1  # only hard below hard

    def test_frozen(self):
        summary = get_economy_summary([])
        with pytest.raises(AttributeError):
            summary.total_peers = 99


class TestPeerEconomy:
    def test_basic_conversion(self):
        row = _ledger_row(balance=5.0, credit_limit=10.0, trust=0.8)
        pe = peer_economy_from_row(row)
        assert pe.balance == 5.0
        assert pe.credit_limit == 10.0
        assert pe.trust == 0.8

    def test_utilization_calculation(self):
        # A1.3 formula: utilization = abs(min(balance, 0)) / abs(hard_limit) * 100
        # 0% = no debt (balance >= 0), 100% = at hard_limit (-10.0 default)
        pe_full = peer_economy_from_row(
            _ledger_row(balance=-10.0, credit_limit=10.0, soft_limit=-5.0)
        )
        assert pe_full.utilization_pct == 100.0

        pe_zero = peer_economy_from_row(
            _ledger_row(balance=10.0, credit_limit=10.0, soft_limit=-5.0)
        )
        assert pe_zero.utilization_pct == 0.0

        pe_half = peer_economy_from_row(
            _ledger_row(balance=-5.0, credit_limit=10.0, soft_limit=-5.0)
        )
        assert pe_half.utilization_pct == 50.0

    def test_utilization_clamped(self):
        """Utilization can't go below 0 or above 100."""
        pe = peer_economy_from_row(
            _ledger_row(balance=20.0, credit_limit=10.0, soft_limit=-5.0)
        )
        assert pe.utilization_pct == 0.0  # clamped at 0

    def test_frozen(self):
        pe = peer_economy_from_row(_ledger_row())
        with pytest.raises(AttributeError):
            pe.balance = 999.0


# ── B4: Admission Pipeline ─────────────────────────────────────────────


class TestAdmissionPipeline:
    def test_accepted_no_discounts(self):
        ctx = AdmissionContext(
            caller_key="a" * 64,
            skill_name="llm-chat",
            base_price=1.0,
            balance=10.0,
            identity="my_node",
            counterparty="their_node",
        )
        result = run_admission(ctx)
        assert result.gate.outcome == "accepted"
        assert result.pricing.final_price == 1.0
        assert result.receipt.document_type == "admission_decision"
        assert result.receipt["decision"]["outcome"] == "accepted"

    def test_accepted_with_discount(self):
        ctx = AdmissionContext(
            caller_key="a" * 64,
            skill_name="llm-chat",
            base_price=100.0,
            balance=100.0,
            discount_rules=[
                DiscountRule(name="felag", group_name="felag_a", skill_group="*", effect_pct=20.0),
            ],
            pricing_config=PricingConfig(discount_mode="multiplicative"),
        )
        result = run_admission(ctx)
        assert result.gate.outcome == "accepted"
        assert result.pricing.final_price == 80.0
        assert result.gate.effective_price == 80.0

    def test_hard_block(self):
        ctx = AdmissionContext(
            caller_key="a" * 64,
            skill_name="expensive",
            base_price=50.0,
            balance=0.0,
            hard_limit=-10.0,
        )
        result = run_admission(ctx)
        assert result.gate.outcome == "hard_block"
        assert result.receipt["decision"]["outcome"] == "hard_block"
        assert result.receipt["decision"]["balance_after"] == -50.0

    def test_soft_warning(self):
        ctx = AdmissionContext(
            caller_key="a" * 64,
            skill_name="llm-chat",
            base_price=3.0,
            balance=-3.0,
            soft_limit=-5.0,
            hard_limit=-10.0,
        )
        result = run_admission(ctx)
        assert result.gate.outcome == "soft_warning"

    def test_free_pass_tit_for_tat(self):
        ctx = AdmissionContext(
            caller_key="a" * 64,
            skill_name="echo",
            base_price=1.0,
            balance=-999.0,
            tit_for_tat=True,
        )
        result = run_admission(ctx)
        assert result.gate.outcome == "free_pass"

    def test_receipt_has_pricing_breakdown(self):
        ctx = AdmissionContext(
            caller_key="a" * 64,
            skill_name="llm-chat",
            base_price=100.0,
            balance=100.0,
            discount_rules=[
                DiscountRule(name="loyalty", group_name="felag_a", skill_group="*", effect_pct=15.0),
            ],
        )
        result = run_admission(ctx)
        pricing_info = result.receipt["pricing"]
        assert pricing_info["base_price"] == 100.0
        assert pricing_info["final_price"] == 85.0
        assert len(pricing_info["rules_applied"]) == 1

    def test_receipt_has_identity(self):
        ctx = AdmissionContext(
            caller_key="a" * 64,
            skill_name="llm-chat",
            base_price=1.0,
            balance=10.0,
            identity="my_node_id",
            counterparty="their_node_id",
        )
        result = run_admission(ctx)
        assert result.receipt["identity"] == "my_node_id"
        assert result.receipt["counterparty"] == "their_node_id"

    def test_receipt_id_prefix(self):
        ctx = AdmissionContext(
            caller_key="a" * 64,
            skill_name="llm-chat",
            base_price=1.0,
            balance=10.0,
        )
        result = run_admission(ctx)
        assert result.receipt["receipt_id"].startswith("adm_")

    def test_discount_affects_gate_decision(self):
        """Big discount can change outcome from block to accepted."""
        ctx_no_discount = AdmissionContext(
            caller_key="a" * 64,
            skill_name="llm-chat",
            base_price=100.0,
            balance=0.0,
            hard_limit=-10.0,
        )
        result_no = run_admission(ctx_no_discount)
        assert result_no.gate.outcome == "hard_block"  # 100 > 10

        ctx_with_discount = AdmissionContext(
            caller_key="a" * 64,
            skill_name="llm-chat",
            base_price=100.0,
            balance=0.0,
            hard_limit=-10.0,
            discount_rules=[
                DiscountRule(name="vip", group_name="vip", skill_group="*", effect_pct=95.0),
            ],
            pricing_config=PricingConfig(min_price=0.01, discount_cap_pct=100.0),
        )
        result_yes = run_admission(ctx_with_discount)
        assert result_yes.gate.outcome == "accepted"  # 5 < 10
        assert result_yes.pricing.final_price == 5.0

    def test_result_frozen(self):
        ctx = AdmissionContext(
            caller_key="a" * 64,
            skill_name="test",
            base_price=1.0,
            balance=10.0,
        )
        result = run_admission(ctx)
        with pytest.raises(AttributeError):
            result.gate = None
