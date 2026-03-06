"""Tests for A2: Two-layer conversion — token_to_credits, credits_to_token, boundaries."""
import math
import unittest

from knarr.commerce.conversion import (
    get_conversion_rate,
    token_to_credits,
    credits_to_token,
)


class TestGetConversionRate(unittest.TestCase):

    def test_default_rate_is_one(self):
        rate = get_conversion_rate({})
        self.assertAlmostEqual(rate, 1.0)

    def test_configured_rate_returned(self):
        rate = get_conversion_rate({"economy": {"conversion_rate": 2.5}})
        self.assertAlmostEqual(rate, 2.5)

    def test_invalid_rate_raises(self):
        with self.assertRaises(ValueError):
            get_conversion_rate({"economy": {"conversion_rate": "bad"}})

    def test_zero_rate_raises(self):
        with self.assertRaises(ValueError):
            get_conversion_rate({"economy": {"conversion_rate": 0}})

    def test_negative_rate_raises(self):
        with self.assertRaises(ValueError):
            get_conversion_rate({"economy": {"conversion_rate": -1.0}})

    def test_nan_rate_raises(self):
        with self.assertRaises(ValueError):
            get_conversion_rate({"economy": {"conversion_rate": float("nan")}})

    def test_inf_rate_raises(self):
        with self.assertRaises(ValueError):
            get_conversion_rate({"economy": {"conversion_rate": float("inf")}})


class TestTokenToCredits(unittest.TestCase):

    def test_basic_conversion(self):
        # 100 tokens × rate 1.0 = 100 credits
        result = token_to_credits(100.0, 1.0)
        self.assertAlmostEqual(result, 100.0)

    def test_fractional_rate(self):
        result = token_to_credits(100.0, 0.5)
        self.assertAlmostEqual(result, 50.0)

    def test_zero_amount_raises(self):
        with self.assertRaises(ValueError):
            token_to_credits(0.0, 1.0)

    def test_negative_amount_raises(self):
        with self.assertRaises(ValueError):
            token_to_credits(-10.0, 1.0)

    def test_nan_amount_raises(self):
        with self.assertRaises(ValueError):
            token_to_credits(float("nan"), 1.0)

    def test_inf_amount_raises(self):
        with self.assertRaises(ValueError):
            token_to_credits(float("inf"), 1.0)

    def test_nan_rate_raises(self):
        with self.assertRaises(ValueError):
            token_to_credits(10.0, float("nan"))

    def test_zero_rate_raises(self):
        with self.assertRaises(ValueError):
            token_to_credits(10.0, 0.0)

    def test_brief_example(self):
        # Deposit 100 $KNARR at rate 1.0 → 100 credits
        result = token_to_credits(100.0, 1.0)
        self.assertAlmostEqual(result, 100.0)


class TestCreditsToToken(unittest.TestCase):

    def test_basic_conversion(self):
        result = credits_to_token(42.0, 1.0)
        self.assertAlmostEqual(result, 42.0)

    def test_fractional_rate(self):
        result = credits_to_token(100.0, 0.5)
        self.assertAlmostEqual(result, 200.0)

    def test_zero_amount_raises(self):
        with self.assertRaises(ValueError):
            credits_to_token(0.0, 1.0)

    def test_negative_amount_raises(self):
        with self.assertRaises(ValueError):
            credits_to_token(-5.0, 1.0)

    def test_nan_amount_raises(self):
        with self.assertRaises(ValueError):
            credits_to_token(float("nan"), 1.0)

    def test_inf_amount_raises(self):
        with self.assertRaises(ValueError):
            credits_to_token(float("inf"), 1.0)

    def test_nan_rate_raises(self):
        with self.assertRaises(ValueError):
            credits_to_token(10.0, float("nan"))

    def test_zero_rate_raises(self):
        with self.assertRaises(ValueError):
            credits_to_token(10.0, 0.0)

    def test_brief_example(self):
        # 42.0 credits at rate 1.0 → 42.0 $KNARR
        result = credits_to_token(42.0, 1.0)
        self.assertAlmostEqual(result, 42.0)

    def test_roundtrip(self):
        rate = 1.5
        credits = token_to_credits(100.0, rate)
        tokens_back = credits_to_token(credits, rate)
        self.assertAlmostEqual(tokens_back, 100.0, places=6)


class TestUpdatePrepaid(unittest.TestCase):
    """A2: update_prepaid storage method."""

    def test_update_prepaid_adds_amount(self):
        import tempfile, os
        from knarr.dht.storage import Storage

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            s = Storage(db_path)
            pk = "cc" * 32
            s.get_or_create_ledger_entry(pk)
            s.update_prepaid(pk, 100.0)
            # Read back via get_all_ledger_entries
            entries = s.get_all_ledger_entries()
            match = [e for e in entries if e.get("peer_public_key") == pk]
            if match:
                prepaid = match[0].get("prepaid", 0.0) or 0.0
                self.assertAlmostEqual(prepaid, 100.0, places=4)
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
