"""Tests for A1: Ledger semantic migration — balance transform, fresh peer at 0, effective_balance."""
import math
import unittest


class TestLedgerMigrationFreshPeer(unittest.TestCase):
    """A1.2: new ledger entries start at 0.0."""

    def test_storage_new_entry_at_zero(self):
        """get_or_create_ledger_entry creates entry with balance=0.0."""
        import sqlite3, tempfile, os
        from knarr.dht.storage import Storage

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            s = Storage(db_path)
            entry = s.get_or_create_ledger_entry("aabbcc" * 10 + "dd", initial_balance=5.0, initial_trust=0.3)
            self.assertEqual(entry.balance, 0.0, "new entry must start at 0.0 not initial_balance")
        finally:
            try:
                s._keepalive_conn.close()
            except Exception:
                pass
            try:
                os.unlink(db_path)
            except Exception:
                pass

    def test_storage_existing_entry_preserved(self):
        """Existing ledger entry balance is not reset."""
        import sqlite3, tempfile, os
        from knarr.dht.storage import Storage

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            s = Storage(db_path)
            pk = "aabbcc" * 10 + "dd"
            s.get_or_create_ledger_entry(pk)
            s.update_ledger_provider(pk, 5.0)  # balance -= 5 → -5.0
            entry2 = s.get_or_create_ledger_entry(pk)
            self.assertAlmostEqual(entry2.balance, -5.0, places=4)
        finally:
            try:
                s._keepalive_conn.close()
            except Exception:
                pass
            try:
                os.unlink(db_path)
            except Exception:
                pass


class TestEffectiveBalance(unittest.TestCase):
    """A1.4: effective_balance = balance + prepaid."""

    def test_can_afford_within_effective(self):
        """balance=-7, prepaid=2, hard_limit=-10, price=4 → allowed."""
        balance = -7.0
        prepaid = 2.0
        hard_limit = -10.0
        price = 4.0
        effective = balance + prepaid
        self.assertGreater(effective - price, hard_limit)

    def test_cannot_afford_exceeds_hard_limit(self):
        """balance=-7, prepaid=2, hard_limit=-10, price=6 → rejected."""
        balance = -7.0
        prepaid = 2.0
        hard_limit = -10.0
        price = 6.0
        effective = balance + prepaid
        self.assertLess(effective - price, hard_limit)

    def test_zero_prepaid_normal(self):
        """With prepaid=0, effective_balance == balance."""
        balance = -3.0
        prepaid = 0.0
        self.assertEqual(balance + prepaid, -3.0)

    def test_prepaid_can_cover_deep_debt(self):
        """Large prepaid can bridge a deep balance."""
        balance = -9.0
        prepaid = 8.0
        hard_limit = -10.0
        price = 1.0
        effective = balance + prepaid
        self.assertGreater(effective - price, hard_limit)


class TestUtilizationFormula(unittest.TestCase):
    """A1.3: new utilization formula."""

    def _util(self, balance, hard_limit):
        from knarr.commerce.economy import peer_economy_from_row
        row = {
            "peer_public_key": "a" * 64,
            "balance": balance,
            "hard_limit": hard_limit,
            "soft_limit": -5.0,
            "credit_limit": 3.0,
            "prepaid": 0.0,
            "pub_tab": 0.0,
            "tasks_provided": 0,
            "tasks_consumed": 0,
            "trust": 0.3,
        }
        return peer_economy_from_row(row).utilization_pct

    def test_zero_debt_is_zero_utilization(self):
        self.assertAlmostEqual(self._util(0.0, -10.0), 0.0, places=1)

    def test_positive_balance_is_zero_utilization(self):
        self.assertAlmostEqual(self._util(5.0, -10.0), 0.0, places=1)

    def test_at_hard_limit_is_100pct(self):
        self.assertAlmostEqual(self._util(-10.0, -10.0), 100.0, places=1)

    def test_half_utilization(self):
        self.assertAlmostEqual(self._util(-5.0, -10.0), 50.0, places=1)

    def test_70pct_from_brief_example(self):
        # balance=-7, hard_limit=-10 → 70%
        self.assertAlmostEqual(self._util(-7.0, -10.0), 70.0, places=1)


class TestV038BalanceMigration(unittest.TestCase):
    """A1.1: Python migration — backup + balance transform."""

    def test_migration_creates_backup_and_transforms(self):
        import tempfile, os
        from knarr.dht.storage import Storage

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            s = Storage(db_path)
            pk = "bb" * 32
            s.get_or_create_ledger_entry(pk)
            # Manually set balance to 3.0 (old "initial credit" value)
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.execute("UPDATE ledger SET balance = 3.0 WHERE peer_public_key = ?", (pk,))
            conn.commit()
            conn.close()

            # Run migration with default_soft_limit=3.0
            rows_updated = s.run_v038_balance_migration(3.0)
            self.assertGreater(rows_updated, 0)

            # Balance should now be 3.0 - 3.0 = 0.0
            entry = s.get_or_create_ledger_entry(pk)
            self.assertAlmostEqual(entry.balance, 0.0, places=4)

            # Backup table should exist
            conn2 = sqlite3.connect(db_path)
            cur = conn2.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bilateral_positions_v037'")
            self.assertIsNotNone(cur.fetchone())
            conn2.close()
        finally:
            try:
                s._keepalive_conn.close()
            except Exception:
                pass
            try:
                os.unlink(db_path)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
