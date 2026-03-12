"""Tests for Track C document types (settlement_prepared/accepted/processed/confirmation)."""

import pytest
from knarr.commerce.documents import (
    Document,
    _TYPE_REGISTRY,
    _PREFIX_MAP,
    settlement_prepared,
    settlement_accepted,
    settlement_processed,
    settlement_confirmation,
)

NODE_ID = "a" * 64
PEER_KEY = "b" * 64


class TestSettlementDocumentRegistry:
    """All 4 settlement types must be in _TYPE_REGISTRY and _PREFIX_MAP."""

    def test_settlement_prepared_registered(self):
        assert "settlement_prepared" in _TYPE_REGISTRY

    def test_settlement_accepted_registered(self):
        assert "settlement_accepted" in _TYPE_REGISTRY

    def test_settlement_processed_registered(self):
        assert "settlement_processed" in _TYPE_REGISTRY

    def test_settlement_confirmation_registered(self):
        assert "settlement_confirmation" in _TYPE_REGISTRY

    def test_settlement_prepared_prefix(self):
        assert _PREFIX_MAP["settlement_prepared"] == "sp"

    def test_settlement_accepted_prefix(self):
        assert _PREFIX_MAP["settlement_accepted"] == "sa"

    def test_settlement_processed_prefix(self):
        assert _PREFIX_MAP["settlement_processed"] == "spr"

    def test_settlement_confirmation_prefix(self):
        assert _PREFIX_MAP["settlement_confirmation"] == "sc"


class TestSettlementPreparedDocument:
    def _make(self, **extra):
        return settlement_prepared(
            proposer=NODE_ID,
            counterparty=PEER_KEY,
            amount=50.0,
            formula="test formula",
            proposer_balance=-8.0,
            counterparty_balance_claimed=8.0,
            utilization=0.85,
            target_utilization=0.5,
            **extra,
        )

    def test_factory_creates_document(self):
        doc = self._make()
        assert isinstance(doc, Document)

    def test_document_type(self):
        doc = self._make()
        assert doc.document_type == "settlement_prepared"

    def test_receipt_id_prefix(self):
        doc = self._make()
        assert doc["receipt_id"].startswith("sp_")

    def test_required_fields_present(self):
        doc = self._make()
        assert doc["proposer"] == NODE_ID
        assert doc["counterparty"] == PEER_KEY
        assert doc["amount"] == 50.0
        assert doc["formula"] == "test formula"
        assert doc["proposer_balance"] == -8.0
        assert doc["counterparty_balance_claimed"] == 8.0
        assert doc["utilization"] == 0.85
        assert doc["target_utilization"] == 0.5

    def test_auto_populated_fields(self):
        doc = self._make()
        assert "receipt_id" in doc
        assert "timestamp" in doc
        assert doc["version"] == 2
        assert doc["document_type"] == "settlement_prepared"

    def test_extra_fields_accepted(self):
        doc = self._make(custom_field="hello")
        assert doc["custom_field"] == "hello"

    def test_missing_required_field_raises(self):
        with pytest.raises(ValueError, match="Missing required fields"):
            Document("settlement_prepared", {
                "proposer": NODE_ID,
                "counterparty": PEER_KEY,
                # amount missing
                "formula": "test",
                "proposer_balance": -8.0,
                "counterparty_balance_claimed": 8.0,
                "utilization": 0.85,
                "target_utilization": 0.5,
            })

    def test_payload_deep_copy(self):
        """payload property must return a defensive copy."""
        doc = self._make()
        payload1 = doc.payload
        payload2 = doc.payload
        payload1["amount"] = 999.0
        assert doc["amount"] == 50.0
        assert payload2["amount"] == 50.0


class TestSettlementAcceptedDocument:
    def _make(self, **extra):
        return settlement_accepted(
            proposer=NODE_ID,
            counterparty=PEER_KEY,
            amount=50.0,
            authority="cockpit-1",
            authority_method=f"did:knarr:{NODE_ID}#cockpit-1",
            prepared_receipt_id="sp_abc123",
            **extra,
        )

    def test_factory_creates_document(self):
        doc = self._make()
        assert doc.document_type == "settlement_accepted"

    def test_receipt_id_prefix(self):
        doc = self._make()
        assert doc["receipt_id"].startswith("sa_")

    def test_required_fields_present(self):
        doc = self._make()
        assert doc["proposer"] == NODE_ID
        assert doc["counterparty"] == PEER_KEY
        assert doc["amount"] == 50.0
        assert doc["authority"] == "cockpit-1"
        assert doc["authority_method"] == f"did:knarr:{NODE_ID}#cockpit-1"
        assert doc["prepared_receipt_id"] == "sp_abc123"

    def test_missing_required_field_raises(self):
        with pytest.raises(ValueError):
            Document("settlement_accepted", {
                "proposer": NODE_ID,
                "counterparty": PEER_KEY,
                "amount": 50.0,
                # authority missing
                "authority_method": "did:knarr:xxx#cockpit-1",
                "prepared_receipt_id": "sp_abc",
            })


class TestSettlementProcessedDocument:
    def _make(self, **extra):
        return settlement_processed(
            proposer=NODE_ID,
            counterparty=PEER_KEY,
            amount_settled=50.0,
            ledger_delta=8.0,
            final_balance=0.0,
            accepted_receipt_id="sa_abc123",
            settle_request_ref="req_001",
            **extra,
        )

    def test_factory_creates_document(self):
        doc = self._make()
        assert doc.document_type == "settlement_processed"

    def test_receipt_id_prefix(self):
        doc = self._make()
        assert doc["receipt_id"].startswith("spr_")

    def test_required_fields_present(self):
        doc = self._make()
        assert doc["proposer"] == NODE_ID
        assert doc["counterparty"] == PEER_KEY
        assert doc["amount_settled"] == 50.0
        assert doc["ledger_delta"] == 8.0
        assert doc["final_balance"] == 0.0
        assert doc["accepted_receipt_id"] == "sa_abc123"
        assert doc["settle_request_ref"] == "req_001"


class TestSettlementConfirmationDocument:
    def _make(self, **extra):
        return settlement_confirmation(
            proposer=NODE_ID,
            counterparty=PEER_KEY,
            amount_confirmed=50.0,
            own_final_balance=0.0,
            processed_receipt_id="spr_abc123",
            **extra,
        )

    def test_factory_creates_document(self):
        doc = self._make()
        assert doc.document_type == "settlement_confirmation"

    def test_receipt_id_prefix(self):
        doc = self._make()
        assert doc["receipt_id"].startswith("sc_")

    def test_required_fields_present(self):
        doc = self._make()
        assert doc["proposer"] == NODE_ID
        assert doc["counterparty"] == PEER_KEY
        assert doc["amount_confirmed"] == 50.0
        assert doc["own_final_balance"] == 0.0
        assert doc["processed_receipt_id"] == "spr_abc123"

    def test_missing_required_field_raises(self):
        with pytest.raises(ValueError):
            Document("settlement_confirmation", {
                "proposer": NODE_ID,
                "counterparty": PEER_KEY,
                "amount_confirmed": 50.0,
                # own_final_balance and processed_receipt_id missing
            })


class TestDocumentUnknownTypeRejected:
    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown document type"):
            Document("nonexistent_type_xyz", {"field": "value"})
