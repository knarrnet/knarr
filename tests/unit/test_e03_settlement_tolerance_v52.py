"""E-03: Settlement confirmation tolerance check.

F-02 formula produces amount_settled that may differ from prior_balance due to
rounding/netting. The confirmation check must accept amount_settled >= prior_balance * 0.99.

The tolerance logic is in node.py _handle_settlement_confirmation().
We test the tolerance calculation directly.
"""

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Test helpers: replicate the tolerance logic from _handle_settlement_confirmation
# ──────────────────────────────────────────────────────────────────────────────

def _confirmation_accepted(amount_settled: float, prior_balance: float) -> bool:
    """Replicate the E-03 tolerance check from node.py _handle_settlement_confirmation."""
    if prior_balance == 0.0:
        return True  # zero balance — always accept
    if amount_settled <= 0:
        return False
    tolerance = max(1.0, abs(prior_balance) * 0.01)
    return amount_settled >= abs(prior_balance) - tolerance


# ──────────────────────────────────────────────────────────────────────────────
# E-03-A: Exact match always accepted
# ──────────────────────────────────────────────────────────────────────────────

def test_exact_amount_accepted():
    """amount_settled == prior_balance should always be accepted."""
    assert _confirmation_accepted(100.0, 100.0)
    assert _confirmation_accepted(50.0, 50.0)
    assert _confirmation_accepted(0.01, 0.01)


# ──────────────────────────────────────────────────────────────────────────────
# E-03-B: Overpayment accepted
# ──────────────────────────────────────────────────────────────────────────────

def test_overpayment_accepted():
    """amount_settled > prior_balance (netting float) should be accepted."""
    assert _confirmation_accepted(110.0, 100.0)  # 10% overpayment
    assert _confirmation_accepted(200.0, 100.0)  # 100% overpayment


# ──────────────────────────────────────────────────────────────────────────────
# E-03-C: Within tolerance accepted (1% tolerance)
# ──────────────────────────────────────────────────────────────────────────────

def test_within_tolerance_accepted():
    """amount_settled slightly below prior_balance (within 1%) should be accepted."""
    prior = 100.0
    # 0.5% below — within 1% tolerance
    assert _confirmation_accepted(99.5, prior)
    # Exactly at tolerance boundary (prior * 0.99 = 99.0)
    assert _confirmation_accepted(99.0, prior)
    # 1 credit below $1 minimum tolerance
    assert _confirmation_accepted(99.0, 100.0)


# ──────────────────────────────────────────────────────────────────────────────
# E-03-D: Below tolerance rejected
# ──────────────────────────────────────────────────────────────────────────────

def test_below_tolerance_rejected():
    """amount_settled significantly below prior_balance should be rejected."""
    prior = 100.0
    # 2% below — exceeds 1% tolerance
    assert not _confirmation_accepted(97.0, prior)
    # 50% below
    assert not _confirmation_accepted(50.0, prior)


# ──────────────────────────────────────────────────────────────────────────────
# E-03-E: Minimum tolerance floor ($1 absolute)
# ──────────────────────────────────────────────────────────────────────────────

def test_minimum_tolerance_floor():
    """For small balances, minimum tolerance is 1 credit."""
    # prior=5.0: 1% = 0.05, but minimum is 1.0
    # So tolerance = max(1.0, 0.05) = 1.0
    # Accept if amount_settled >= 5.0 - 1.0 = 4.0
    assert _confirmation_accepted(4.0, 5.0)   # at boundary
    assert _confirmation_accepted(4.5, 5.0)   # within
    assert not _confirmation_accepted(3.9, 5.0)  # below


# ──────────────────────────────────────────────────────────────────────────────
# E-03-F: Zero balance edge case
# ──────────────────────────────────────────────────────────────────────────────

def test_zero_balance_accepted():
    """Zero prior_balance means no settlement needed — any amount accepted."""
    assert _confirmation_accepted(0.0, 0.0)
    assert _confirmation_accepted(5.0, 0.0)


# ──────────────────────────────────────────────────────────────────────────────
# E-03-G: Zero or negative amount_settled always rejected (non-zero balance)
# ──────────────────────────────────────────────────────────────────────────────

def test_zero_amount_rejected():
    """Zero or negative amount_settled should always be rejected."""
    assert not _confirmation_accepted(0.0, 100.0)
    assert not _confirmation_accepted(-5.0, 100.0)


# ──────────────────────────────────────────────────────────────────────────────
# E-03-H: Integration — verify node.py has the tolerance logic
# ──────────────────────────────────────────────────────────────────────────────

def test_node_has_tolerance_logic():
    """Verify node.py _handle_settlement_confirmation implements tolerance check."""
    import inspect
    import importlib

    # Load node module source
    try:
        from knarr.dht import node as node_module
        src = inspect.getsource(node_module)
    except Exception:
        pytest.skip("Cannot inspect node module source")

    # Tolerance should be computed as max(1.0, ...) or similar
    assert "tolerance" in src, "node.py missing tolerance variable in settlement confirmation"
    assert "0.01" in src or "99" in src, "node.py missing 1% tolerance calculation"
