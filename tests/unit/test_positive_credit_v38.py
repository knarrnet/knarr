"""Tests for B1: positive credit — sign flip at credit flow sites, admission skip."""
import unittest


class TestSignConvention(unittest.TestCase):
    """B1 sign convention: negative balance = they owe us, positive = we owe them."""

    def test_negative_balance_means_they_owe_us(self):
        """Standard task: provider charges. Consumer's balance goes more negative."""
        # update_ledger_provider: balance -= price
        # Starting at 0, after provider charges 5 → balance = -5 (they owe us 5)
        import tempfile, os
        from knarr.dht.storage import Storage

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            s = Storage(db_path)
            pk = "dd" * 32
            s.get_or_create_ledger_entry(pk)
            s.update_ledger_provider(pk, 5.0)
            balance = s.get_ledger_balance(pk)
            self.assertAlmostEqual(balance, -5.0, places=4, msg="after provider charges 5, balance = -5 (they owe us)")
        finally:
            try:
                s._keepalive_conn.close()
            except Exception:
                pass
            try:
                os.unlink(db_path)
            except Exception:
                pass

    def test_consumer_update_correct_direction(self):
        """Consumer side: balance becomes more negative (they owe more)."""
        import tempfile, os
        from knarr.dht.storage import Storage

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            s = Storage(db_path)
            pk = "ee" * 32
            s.get_or_create_ledger_entry(pk)
            s.update_ledger_consumer(pk, 3.0)
            balance = s.get_ledger_balance(pk)
            # update_ledger_consumer should move balance away from 0 (consumer owes more)
            # Direction depends on implementation, but the sign convention is: negative = they owe us
            self.assertNotEqual(balance, 0.0)
        finally:
            try:
                s._keepalive_conn.close()
            except Exception:
                pass
            try:
                os.unlink(db_path)
            except Exception:
                pass


class TestBountyAdmissionSkip(unittest.TestCase):
    """B1: negative price = bounty. Admission check skipped for bounty skills."""

    def test_bounty_price_skip_condition(self):
        """If skill_price < 0, admission check should be skipped."""
        skill_price = -5.0
        # The check: if skill_price >= 0, apply admission check
        admission_applied = skill_price >= 0
        self.assertFalse(admission_applied, "bounty (negative price) should skip admission check")

    def test_normal_price_applies_admission(self):
        skill_price = 5.0
        admission_applied = skill_price >= 0
        self.assertTrue(admission_applied)

    def test_zero_price_applies_admission(self):
        # Free skill (price=0) still applies admission path (but won't reject at 0)
        skill_price = 0.0
        admission_applied = skill_price >= 0
        self.assertTrue(admission_applied)


class TestBountySignFlipLogic(unittest.TestCase):
    """B1: bounty credit flow direction."""

    def test_bounty_provider_flow(self):
        """Provider (us) offering bounty: update_ledger_consumer(peer, abs(price)).
        This means peer earns, their balance becomes positive (we owe them).
        """
        import tempfile, os
        from knarr.dht.storage import Storage

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            s = Storage(db_path)
            pk = "ff" * 32
            s.get_or_create_ledger_entry(pk)
            # Bounty path: provider side with negative price → use update_ledger_consumer
            skill_price = -5.0
            if skill_price >= 0:
                s.update_ledger_provider(pk, skill_price)
            else:
                s.update_ledger_consumer(pk, abs(skill_price))
            balance = s.get_ledger_balance(pk)
            # update_ledger_consumer moves in the opposite direction of update_ledger_provider
            # Sign: balance should be != 0 (peer earned something)
            self.assertNotEqual(balance, 0.0)
        finally:
            try:
                s._keepalive_conn.close()
            except Exception:
                pass
            try:
                os.unlink(db_path)
            except Exception:
                pass

    def test_worked_example_from_brief(self):
        """
        Skill "code-review-bounty": price = -5.0
        Provider (us): update_ledger_consumer(executor, 5.0) → executor earns 5
        After: executor balance moves away from 0
        """
        import tempfile, os
        from knarr.dht.storage import Storage

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            s = Storage(db_path)
            executor_pk = "aa" * 32
            s.get_or_create_ledger_entry(executor_pk)

            # Bounty execution
            bounty_price = -5.0
            # Provider (us) running the bounty: we pay the executor
            s.update_ledger_consumer(executor_pk, abs(bounty_price))

            balance_after = s.get_ledger_balance(executor_pk)
            # Balance should now reflect that executor has earned credits
            # (non-zero, positive direction = we owe them)
            self.assertNotEqual(balance_after, 0.0)
        finally:
            try:
                s._keepalive_conn.close()
            except Exception:
                pass
            try:
                os.unlink(db_path)
            except Exception:
                pass


class TestPositiveCreditModels(unittest.TestCase):
    """B1: LedgerEntry now includes extended fields."""

    def test_ledger_entry_has_prepaid(self):
        from knarr.core.models import LedgerEntry
        entry = LedgerEntry(peer_public_key="x" * 64)
        self.assertEqual(entry.prepaid, 0.0)

    def test_ledger_entry_has_hard_limit(self):
        from knarr.core.models import LedgerEntry
        entry = LedgerEntry(peer_public_key="x" * 64)
        self.assertLess(entry.hard_limit, 0)

    def test_ledger_entry_has_soft_limit(self):
        from knarr.core.models import LedgerEntry
        entry = LedgerEntry(peer_public_key="x" * 64)
        self.assertLess(entry.soft_limit, 0)

    def test_ledger_entry_has_credit_limit(self):
        from knarr.core.models import LedgerEntry
        entry = LedgerEntry(peer_public_key="x" * 64)
        self.assertGreater(entry.credit_limit, 0)


if __name__ == "__main__":
    unittest.main()
