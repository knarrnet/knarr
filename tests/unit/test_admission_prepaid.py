import pytest

from knarr.commerce.admission_pipeline import AdmissionContext, run_admission


def _context(**overrides):
    base = {
        "caller_key": "a" * 64,
        "skill_name": "demo",
        "base_price": 2.5,
        "balance": -9.0,
        "soft_limit": -5.0,
        "hard_limit": -10.0,
    }
    base.update(overrides)
    return AdmissionContext(**base)


def test_run_admission_uses_prepaid_balance_before_gate():
    result = run_admission(_context(prepaid_balance=10.0))
    assert result.prepaid is not None
    assert result.prepaid.action == "deduct"
    assert result.gate.outcome == "accepted"
    assert result.gate.effective_price == 0.0
    assert result.receipt["decision"]["prepaid_action"] == "deduct"


def test_run_admission_blocks_when_prepaid_is_insufficient():
    result = run_admission(_context(prepaid_balance=1.0))
    assert result.prepaid is not None
    assert result.prepaid.action == "reject"
    assert result.gate.outcome == "hard_block"
    assert "Prepaid rejection" in (result.gate.reason or "")


def test_run_admission_falls_back_to_credit_when_no_prepaid_balance():
    result = run_admission(_context(prepaid_balance=0.0, balance=10.0))
    assert result.prepaid is not None
    assert result.prepaid.action == "skip"
    assert result.gate.outcome == "accepted"
    assert result.gate.effective_price == pytest.approx(2.5)
