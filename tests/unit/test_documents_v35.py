"""Tests for Document construction layer (Layer 1)."""

import json
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from knarr.commerce.documents import (
    Document,
    execution_receipt,
    credit_note,
    order_ack,
    order_executing,
    mail_delivery_receipt,
    mail_receive_receipt,
    admission_decision,
    price_calculation,
    _TYPE_REGISTRY,
    _PREFIX_MAP,
)


class TestDocumentConstruction:
    def test_valid_execution_receipt(self):
        doc = Document("execution_receipt", {
            "skill_name": "llm-chat", "provider": "aaa",
            "consumer": "bbb", "status": "completed",
        })
        assert doc.document_type == "execution_receipt"
        assert doc["skill_name"] == "llm-chat"

    def test_missing_required_field_raises(self):
        with pytest.raises(ValueError, match="Missing required fields"):
            Document("execution_receipt", {
                "skill_name": "llm-chat", "provider": "aaa",
                # missing: consumer, status
            })

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown document type"):
            Document("bogus_type", {"field": "value"})

    def test_extra_fields_accepted(self):
        """CloudEvents pattern: extensions are implicit."""
        doc = Document("order_ack", {
            "skill_name": "llm-chat", "status": "accepted",
            "extra_field": 42, "another": "value",
        })
        assert doc["extra_field"] == 42
        assert doc["another"] == "value"


class TestAutoDefaults:
    def test_receipt_id_generated(self):
        doc = order_ack(skill_name="test", status="accepted")
        assert doc["receipt_id"].startswith("oack_")
        assert len(doc["receipt_id"]) == 5 + 16  # "oack_" + 16 hex chars

    def test_timestamp_generated(self):
        doc = order_ack(skill_name="test", status="accepted")
        ts = doc["timestamp"]
        assert ts.endswith("Z")
        assert "T" in ts

    def test_version_is_2(self):
        doc = order_ack(skill_name="test", status="accepted")
        assert doc["version"] == 2

    def test_document_type_in_payload(self):
        doc = order_ack(skill_name="test", status="accepted")
        assert doc["document_type"] == "order_ack"

    def test_unique_receipt_ids(self):
        doc1 = order_ack(skill_name="test", status="accepted")
        doc2 = order_ack(skill_name="test", status="accepted")
        assert doc1["receipt_id"] != doc2["receipt_id"]


class TestPrefixMap:
    def test_all_types_have_prefix(self):
        for doc_type in _TYPE_REGISTRY:
            assert doc_type in _PREFIX_MAP, f"{doc_type} missing from _PREFIX_MAP"

    def test_admission_prefix(self):
        assert _PREFIX_MAP["admission_decision"] == "adm"

    def test_credit_note_prefix(self):
        assert _PREFIX_MAP["credit_note"] == "cn"


class TestMappingProtocol:
    def test_getitem(self):
        doc = order_ack(skill_name="test", status="ok")
        assert doc["skill_name"] == "test"

    def test_contains(self):
        doc = order_ack(skill_name="test", status="ok")
        assert "skill_name" in doc
        assert "nonexistent" not in doc

    def test_get_with_default(self):
        doc = order_ack(skill_name="test", status="ok")
        assert doc.get("missing", "default") == "default"
        assert doc.get("skill_name") == "test"

    def test_getitem_missing_raises(self):
        doc = order_ack(skill_name="test", status="ok")
        with pytest.raises(KeyError):
            _ = doc["nonexistent"]


class TestPayloadCopy:
    def test_payload_returns_copy(self):
        doc = order_ack(skill_name="test", status="ok")
        p1 = doc.payload
        p2 = doc.payload
        assert p1 == p2
        assert p1 is not p2

    def test_mutating_payload_does_not_affect_document(self):
        doc = order_ack(skill_name="test", status="ok")
        p = doc.payload
        p["skill_name"] = "hacked"
        assert doc["skill_name"] == "test"


class TestCanonicalJson:
    def test_canonical_json_is_string(self):
        doc = order_ack(skill_name="test", status="ok")
        cj = doc.canonical_json()
        assert isinstance(cj, str)

    def test_canonical_json_is_valid_json(self):
        doc = order_ack(skill_name="test", status="ok")
        parsed = json.loads(doc.canonical_json())
        assert parsed["skill_name"] == "test"

    def test_canonical_json_deterministic(self):
        # Same document produces same canonical form
        doc = Document("order_ack", {"skill_name": "z", "status": "a", "extra_b": 1, "extra_a": 2})
        cj1 = doc.canonical_json()
        cj2 = doc.canonical_json()
        assert cj1 == cj2

    def test_canonical_json_keys_sorted(self):
        doc = Document("order_ack", {"skill_name": "z", "status": "a"})
        cj = doc.canonical_json()
        # In JCS, keys are sorted — "document_type" < "receipt_id" < "skill_name" < "status" < ...
        assert cj.index('"document_type"') < cj.index('"skill_name"')


class TestFactories:
    def test_execution_receipt_factory(self):
        doc = execution_receipt(provider="a", consumer="b", skill_name="s", status="ok", wall_ms=100)
        assert doc.document_type == "execution_receipt"
        assert doc["wall_ms"] == 100

    def test_credit_note_factory(self):
        doc = credit_note(provider="a", consumer="b", skill_name="s", amount=1.5)
        assert doc["amount"] == 1.5

    def test_order_executing_factory(self):
        doc = order_executing(skill_name="s", task_id="t123")
        assert doc["task_id"] == "t123"

    def test_mail_delivery_receipt_factory(self):
        doc = mail_delivery_receipt(recipient="r", message_id="m1", status="delivered")
        assert doc["recipient"] == "r"

    def test_mail_receive_receipt_factory(self):
        doc = mail_receive_receipt(sender="s", message_id="m1")
        assert doc["sender"] == "s"

    def test_admission_decision_factory(self):
        doc = admission_decision(
            skill_name="s",
            decision={"outcome": "accepted", "effective_price": 1.0},
            sanctions={"limit_type": "soft"},  # extra field via **kwargs
            identity="node1",
            counterparty="node2",
        )
        assert doc["decision"]["outcome"] == "accepted"
        assert doc["sanctions"]["limit_type"] == "soft"
        assert doc["identity"] == "node1"
        assert doc["receipt_id"].startswith("adm_")

    def test_price_calculation_factory(self):
        doc = price_calculation(
            skill_name="s",
            inputs={"base_price": 100},
            calculation={"step_1": 100},
            output={"effective_price": 66.53},
        )
        assert doc["output"]["effective_price"] == 66.53
