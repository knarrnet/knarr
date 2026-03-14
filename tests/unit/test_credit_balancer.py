"""Tests for credit balancer — decay_stale_balances."""
import time
import pytest
from knarr.dht.storage import Storage


def test_decay_stale_balances_decays_old_entries():
    """Stale entries get their balance multiplied by (1 - rate)."""
    s = Storage(":memory:")
    # Create an entry — A1.2 always stores balance=0.0 in the DB regardless of initial_balance.
    # Set the real test balance and backdate last_updated via direct SQL (same approach).
    s.get_or_create_ledger_entry("peer_a", 0.0)
    conn = s._get_conn()
    conn.execute("UPDATE ledger SET last_updated = ?, balance = 10.0 WHERE peer_public_key = ?",
                 (time.time() - 7200, "peer_a"))
    conn.commit()

    decayed = s.decay_stale_balances(decay_rate=0.1, stale_seconds=3600)
    assert decayed == 1

    balance = s.get_ledger_balance("peer_a")
    assert abs(balance - 9.0) < 0.01  # 10.0 * 0.9 = 9.0


def test_decay_skips_recent_entries():
    """Recently active entries are not decayed."""
    s = Storage(":memory:")
    # A1.2 always stores balance=0.0; set actual test balance via direct SQL.
    s.get_or_create_ledger_entry("peer_b", 0.0)
    conn = s._get_conn()
    conn.execute("UPDATE ledger SET balance = 5.0 WHERE peer_public_key = ?", ("peer_b",))
    conn.commit()
    # last_updated is now() — should NOT be decayed

    decayed = s.decay_stale_balances(decay_rate=0.5, stale_seconds=3600)
    assert decayed == 0

    balance = s.get_ledger_balance("peer_b")
    assert abs(balance - 5.0) < 0.01


def test_decay_skips_near_zero_balances():
    """Balances close to zero (< 0.01) are not touched."""
    s = Storage(":memory:")
    s.get_or_create_ledger_entry("peer_c", 0.005)
    conn = s._get_conn()
    conn.execute("UPDATE ledger SET last_updated = ? WHERE peer_public_key = ?",
                 (time.time() - 7200, "peer_c"))
    conn.commit()

    decayed = s.decay_stale_balances(decay_rate=0.5, stale_seconds=3600)
    assert decayed == 0


def test_decay_handles_negative_balances():
    """Negative balances (debt) also decay toward zero."""
    s = Storage(":memory:")
    # A1.2 always stores balance=0.0; set the negative test balance via direct SQL.
    s.get_or_create_ledger_entry("peer_d", 0.0)
    conn = s._get_conn()
    conn.execute("UPDATE ledger SET last_updated = ?, balance = -8.0 WHERE peer_public_key = ?",
                 (time.time() - 7200, "peer_d"))
    conn.commit()

    decayed = s.decay_stale_balances(decay_rate=0.1, stale_seconds=3600)
    assert decayed == 1

    balance = s.get_ledger_balance("peer_d")
    assert abs(balance - (-7.2)) < 0.01  # -8.0 * 0.9 = -7.2
