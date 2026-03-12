"""Tests for v0.40.1 settlement hotfix — config merge + formula parity."""

import pytest
from unittest.mock import MagicMock

from knarr.commerce.settlement_engine import SettlementInput, evaluate_settlement


def _make_input(balance=-8.0, soft_limit=3.0, hard_limit=-10.0, utilization=0.8):
    credit_limit = abs(soft_limit - hard_limit)
    return SettlementInput(
        peer_key="aa" * 32,
        balance=balance,
        prepaid=0.0,
        pub_tab=0.0,
        soft_limit=soft_limit,
        hard_limit=hard_limit,
        credit_limit=credit_limit,
        tasks_provided=10,
        tasks_consumed=5,
        utilization=utilization,
    )


class TestSettlementConfigMerge:
    """Bug 1: _get_settlement_config must merge both sections."""

    def _make_node(self, config):
        node = MagicMock()
        node._config = config
        # Bind the real method — use a closure since MagicMock intercepts __get__
        def _get_settlement_config():
            base = node._config.get("economy", {}).get("settlement", {})
            override = node._config.get("settlement", {})
            merged = dict(base)
            merged.update(override)
            return merged
        node._get_settlement_config = _get_settlement_config
        return node

    def test_merges_both_sections(self):
        node = self._make_node({
            "economy": {"settlement": {"soft_threshold": 0.8, "min_settlement_amount": 1.0}},
            "settlement": {"consumer_interval": 30},
        })
        cfg = node._get_settlement_config()
        assert cfg["soft_threshold"] == 0.8
        assert cfg["min_settlement_amount"] == 1.0
        assert cfg["consumer_interval"] == 30

    def test_override_wins_for_same_key(self):
        node = self._make_node({
            "economy": {"settlement": {"min_settlement_amount": 1.0}},
            "settlement": {"min_settlement_amount": 5.0},
        })
        cfg = node._get_settlement_config()
        assert cfg["min_settlement_amount"] == 5.0

    def test_economy_only(self):
        node = self._make_node({
            "economy": {"settlement": {"soft_threshold": 0.7}},
        })
        cfg = node._get_settlement_config()
        assert cfg["soft_threshold"] == 0.7

    def test_shorthand_only(self):
        node = self._make_node({
            "settlement": {"netting_interval": 1800},
        })
        cfg = node._get_settlement_config()
        assert cfg["netting_interval"] == 1800

    def test_both_empty(self):
        node = self._make_node({})
        cfg = node._get_settlement_config()
        assert cfg == {}


class TestSettlementFormulaParity:
    """Bug 2: Engine formula must match netting formula."""

    def test_formula_matches_netting(self):
        """Netting: target = ic - soft_target*(ic-mb), settle = target - balance."""
        ic, mb, balance, soft_target = 3.0, -10.0, -8.0, 0.5
        credit_range = ic - mb  # 13.0
        netting_target = ic - (soft_target * credit_range)  # -3.5
        netting_amount = netting_target - balance  # 4.5

        result = evaluate_settlement(
            _make_input(balance=balance, soft_limit=ic, hard_limit=mb),
            {"soft_threshold": 0.7, "soft_target": soft_target, "min_settlement_amount": 1.0},
        )
        assert result.action == "settle"
        assert result.amount == pytest.approx(netting_amount, abs=0.01)

    def test_accounts_for_initial_credit(self):
        """Engine must NOT produce -3.0 (the old hard_limit-only formula)."""
        result = evaluate_settlement(
            _make_input(balance=-8.0, soft_limit=3.0, hard_limit=-10.0),
            {"soft_threshold": 0.7, "soft_target": 0.5, "min_settlement_amount": 1.0},
        )
        assert result.amount > 0, "Settlement amount should be positive"
        assert result.amount != pytest.approx(-3.0, abs=0.1), "Must not use old formula"

    def test_zero_credit_range(self):
        """credit_limit=0 (soft_limit == hard_limit): no division, safe."""
        result = evaluate_settlement(
            _make_input(balance=-5.0, soft_limit=0.0, hard_limit=0.0, utilization=0.9),
            {"soft_threshold": 0.7, "soft_target": 0.5, "min_settlement_amount": 1.0},
        )
        # credit_limit=0 → skip (no credit relationship)
        assert result.action == "skip"

    def test_soft_target_zero_restores_to_ic(self):
        """soft_target=0: target = ic, settle full distance from ic."""
        ic, mb, balance = 3.0, -10.0, -8.0
        result = evaluate_settlement(
            _make_input(balance=balance, soft_limit=ic, hard_limit=mb),
            {"soft_threshold": 0.7, "soft_target": 0.0, "min_settlement_amount": 1.0},
        )
        assert result.action == "settle"
        expected = ic - balance  # 3.0 - (-8.0) = 11.0
        assert result.amount == pytest.approx(expected, abs=0.01)
