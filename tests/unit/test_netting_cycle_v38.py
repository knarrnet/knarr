"""Tests for A5.6: 5-step netting cycle — happy path, mismatch, dedup."""
import math
import time
import unittest

from knarr.commerce.documents import (
    netting_reconcile, netting_proposal, netting_acceptance, netting_executed,
)
from knarr.commerce.schemas import (
    validate_netting_reconcile, validate_netting_proposal,
    validate_netting_acceptance, validate_netting_executed,
)


class TestNettingDocuments(unittest.TestCase):
    """Document factories and schema validators for all 4 netting types."""

    def test_reconcile_factory(self):
        doc = netting_reconcile(
            netting_id="nr01",
            identity="node-a",
            counterparty="node-b",
            proposed_net=-42.0,
            receipt_count=73,
            chain_id="solana-devnet",
        )
        self.assertEqual(doc["netting_id"], "nr01")
        self.assertEqual(doc["proposed_net"], -42.0)
        self.assertTrue(doc["receipt_id"].startswith("nr_"))

    def test_proposal_factory(self):
        doc = netting_proposal(
            netting_id="np01",
            identity="node-a",
            counterparty="node-b",
            settlement_amount=42.0,
            chain_id="solana-devnet",
            token_mint="",
            target_address="solana-addr-1111",
            deadline="2026-03-07T00:00:00Z",
        )
        self.assertEqual(doc["settlement_amount"], 42.0)
        self.assertTrue(doc["receipt_id"].startswith("np_"))

    def test_acceptance_factory(self):
        doc = netting_acceptance(
            netting_id="na01",
            proposal_ref="np_abc",
            identity="node-b",
            counterparty="node-a",
            accepted_amount=42.0,
            source_address="solana-addr-2222",
        )
        self.assertEqual(doc["accepted_amount"], 42.0)
        self.assertTrue(doc["receipt_id"].startswith("na_"))

    def test_executed_factory(self):
        doc = netting_executed(
            netting_id="ne01",
            acceptance_ref="na_abc",
            identity="node-b",
            counterparty="node-a",
            tx_hash="5xhash123",
            chain_id="solana-devnet",
            amount=42.0,
        )
        self.assertEqual(doc["tx_hash"], "5xhash123")
        self.assertTrue(doc["receipt_id"].startswith("ne_"))


class TestNettingSchemaValidators(unittest.TestCase):

    def test_valid_reconcile(self):
        body = {
            "netting_id": "nr01",
            "identity": "node-a",
            "counterparty": "node-b",
            "proposed_net": -42.0,
            "receipt_count": 73,
            "chain_id": "solana-devnet",
        }
        ok, err = validate_netting_reconcile(body)
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_reconcile_missing_field(self):
        body = {"netting_id": "nr01", "proposed_net": -42.0}
        ok, err = validate_netting_reconcile(body)
        self.assertFalse(ok)
        self.assertIn("missing", err)

    def test_reconcile_nan_proposed_net(self):
        body = {
            "netting_id": "nr01", "identity": "a", "counterparty": "b",
            "proposed_net": float("nan"), "receipt_count": 1, "chain_id": "solana-devnet",
        }
        ok, err = validate_netting_reconcile(body)
        self.assertFalse(ok)

    def test_reconcile_negative_receipt_count_fails(self):
        body = {
            "netting_id": "nr01", "identity": "a", "counterparty": "b",
            "proposed_net": -10.0, "receipt_count": -1, "chain_id": "solana-devnet",
        }
        ok, err = validate_netting_reconcile(body)
        self.assertFalse(ok)

    def test_valid_proposal(self):
        body = {
            "netting_id": "np01",
            "identity": "node-a",
            "counterparty": "node-b",
            "settlement_amount": 42.0,
            "chain_id": "solana-devnet",
            "token_mint": "",
            "target_address": "solana-addr",
            "deadline": "2026-03-07T00:00:00Z",
        }
        ok, err = validate_netting_proposal(body)
        self.assertTrue(ok)

    def test_proposal_zero_amount_fails(self):
        body = {
            "netting_id": "np01", "identity": "a", "counterparty": "b",
            "settlement_amount": 0.0, "chain_id": "x", "token_mint": "",
            "target_address": "addr", "deadline": "2026-01-01",
        }
        ok, err = validate_netting_proposal(body)
        self.assertFalse(ok)

    def test_proposal_negative_amount_fails(self):
        body = {
            "netting_id": "np01", "identity": "a", "counterparty": "b",
            "settlement_amount": -5.0, "chain_id": "x", "token_mint": "",
            "target_address": "addr", "deadline": "2026-01-01",
        }
        ok, err = validate_netting_proposal(body)
        self.assertFalse(ok)

    def test_valid_acceptance(self):
        body = {
            "netting_id": "na01",
            "proposal_ref": "np_abc",
            "identity": "node-b",
            "counterparty": "node-a",
            "accepted_amount": 42.0,
            "source_address": "solana-addr",
        }
        ok, err = validate_netting_acceptance(body)
        self.assertTrue(ok)

    def test_acceptance_inf_amount_fails(self):
        body = {
            "netting_id": "na01", "proposal_ref": "ref", "identity": "b", "counterparty": "a",
            "accepted_amount": float("inf"), "source_address": "addr",
        }
        ok, err = validate_netting_acceptance(body)
        self.assertFalse(ok)

    def test_valid_executed(self):
        body = {
            "netting_id": "ne01",
            "acceptance_ref": "na_abc",
            "identity": "node-b",
            "counterparty": "node-a",
            "tx_hash": "abc123",
            "chain_id": "solana-devnet",
            "amount": 42.0,
        }
        ok, err = validate_netting_executed(body)
        self.assertTrue(ok)

    def test_executed_missing_tx_hash(self):
        body = {
            "netting_id": "ne01", "acceptance_ref": "na_abc",
            "identity": "a", "counterparty": "b",
            "chain_id": "x", "amount": 42.0,
        }
        ok, err = validate_netting_executed(body)
        self.assertFalse(ok)


class TestNettingWMRegistration(unittest.TestCase):
    """A5.3: netting types registered in WM _DEFAULT_RULES."""

    def test_all_netting_types_in_rules(self):
        from knarr.core.warehouse_manager import _DEFAULT_RULES
        for doc_type in ["netting_reconcile", "netting_proposal", "netting_acceptance", "netting_executed"]:
            self.assertIn(doc_type, _DEFAULT_RULES, f"{doc_type} missing from _DEFAULT_RULES")

    def test_reconcile_is_auto_promote(self):
        from knarr.core.warehouse_manager import _DEFAULT_RULES
        self.assertEqual(_DEFAULT_RULES["netting_reconcile"]["action"], "auto_promote")

    def test_proposal_is_hold_for_review(self):
        from knarr.core.warehouse_manager import _DEFAULT_RULES
        self.assertEqual(_DEFAULT_RULES["netting_proposal"]["action"], "hold_for_review")

    def test_acceptance_is_hold_for_review(self):
        from knarr.core.warehouse_manager import _DEFAULT_RULES
        self.assertEqual(_DEFAULT_RULES["netting_acceptance"]["action"], "hold_for_review")

    def test_executed_is_auto_promote(self):
        from knarr.core.warehouse_manager import _DEFAULT_RULES
        self.assertEqual(_DEFAULT_RULES["netting_executed"]["action"], "auto_promote")

    def test_all_netting_types_have_schema_validators(self):
        from knarr.core.warehouse_manager import _get_schema_validators
        validators = _get_schema_validators()
        for doc_type in ["netting_reconcile", "netting_proposal", "netting_acceptance", "netting_executed"]:
            self.assertIn(doc_type, validators)
            self.assertIsNotNone(validators[doc_type])


if __name__ == "__main__":
    unittest.main()
