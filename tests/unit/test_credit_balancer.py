"""Tests for credit balancer — decay_stale_balances."""
import time
import pytest
from knarr.dht.storage import Storage


def test_decay_stale_balances_decays_old_entries():
    """Stale entries get their balance multiplied by (1 - rate)."""
    s = Storage(":memory:")
    # Create an entry with known balance
    s.get_or_create_ledger_entry("peer_a", 10.0)
    # Backdate last_updated so it appears stale
    conn = s._get_conn()
    conn.execute("UPDATE ledger SET last_updated = ? WHERE peer_public_key = ?",
                 (time.time() - 7200, "peer_a"))
    conn.commit()

    decayed = s.decay_stale_balances(decay_rate=0.1, stale_seconds=3600)
    assert decayed == 1

    balance = s.get_ledger_balance("peer_a")
    assert abs(balance - 9.0) < 0.01  # 10.0 * 0.9 = 9.0


def test_decay_skips_recent_entries():
    """Recently active entries are not decayed."""
    s = Storage(":memory:")
    s.get_or_create_ledger_entry("peer_b", 5.0)
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
    s.get_or_create_ledger_entry("peer_d", -8.0)
    conn = s._get_conn()
    conn.execute("UPDATE ledger SET last_updated = ? WHERE peer_public_key = ?",
                 (time.time() - 7200, "peer_d"))
    conn.commit()

    decayed = s.decay_stale_balances(decay_rate=0.1, stale_seconds=3600)
    assert decayed == 1

    balance = s.get_ledger_balance("peer_d")
    assert abs(balance - (-7.2)) < 0.01  # -8.0 * 0.9 = -7.2
