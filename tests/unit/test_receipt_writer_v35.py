"""Tests for receipt_writer — bridges Document layer to storage layer."""

import json
import pytest
from unittest.mock import MagicMock, patch
from nacl.signing import SigningKey

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from knarr.commerce.documents import (
    execution_receipt, credit_note, order_ack,
    mail_delivery_receipt, mail_receive_receipt,
)
from knarr.commerce.receipt_writer import write_receipt


@pytest.fixture
def mock_storage():
    s = MagicMock()
    s.write_receipt = MagicMock()
    return s


@pytest.fixture
def mock_bus():
    b = MagicMock()
    b.emit = MagicMock()
    return b


@pytest.fixture
def signing_key():
    return SigningKey.generate()


IDENTITY = "a" * 64


class TestWriteReceipt:
    def test_returns_receipt_id(self, mock_storage):
        doc = order_ack(skill_name="test", status="accepted")
        rid = write_receipt(doc, IDENTITY, None, mock_storage)
        assert rid == doc["receipt_id"]
        assert rid.startswith("oack_")

    def test_calls_storage(self, mock_storage):
        doc = order_ack(skill_name="test", status="accepted")
        write_receipt(doc, IDENTITY, None, mock_storage, counterparty="bbb", order_ref="job1")

        mock_storage.write_receipt.assert_called_once()
        call_kwargs = mock_storage.write_receipt.call_args[1]
        assert call_kwargs["receipt_id"] == doc["receipt_id"]
        assert call_kwargs["document_type"] == "order_ack"
        assert call_kwargs["identity"] == IDENTITY
        assert call_kwargs["counterparty"] == "bbb"
        assert call_kwargs["order_ref"] == "job1"
        assert call_kwargs["signature"] is None

    def test_unsigned_uses_canonical_json(self, mock_storage):
        doc = order_ack(skill_name="test", status="ok")
        write_receipt(doc, IDENTITY, None, mock_storage)

        call_kwargs = mock_storage.write_receipt.call_args[1]
        payload_json = call_kwargs["payload_json"]
        # Should be valid JSON
        parsed = json.loads(payload_json)
        assert parsed["skill_name"] == "test"
        # JCS: keys sorted, no spaces
        assert '"document_type"' in payload_json
        assert "  " not in payload_json

    def test_signed_includes_proof(self, mock_storage, signing_key):
        doc = execution_receipt(
            provider="aaa", consumer="bbb", skill_name="s", status="ok"
        )
        write_receipt(doc, IDENTITY, signing_key, mock_storage, sign=True)

        call_kwargs = mock_storage.write_receipt.call_args[1]
        assert call_kwargs["signature"] is not None
        assert call_kwargs["signature"].startswith("z")
        # payload_json should contain proof object
        parsed = json.loads(call_kwargs["payload_json"])
        assert "proof" in parsed
        assert parsed["proof"]["type"] == "DataIntegrityProof"

    def test_signed_proof_verifiable(self, mock_storage, signing_key):
        from knarr.core.proof import verify_document

        doc = execution_receipt(
            provider="aaa", consumer="bbb", skill_name="s", status="ok"
        )
        write_receipt(doc, IDENTITY, signing_key, mock_storage, sign=True)

        call_kwargs = mock_storage.write_receipt.call_args[1]
        secured = json.loads(call_kwargs["payload_json"])
        assert verify_document(secured, signing_key.verify_key) is True

    def test_sign_false_no_proof(self, mock_storage, signing_key):
        doc = order_ack(skill_name="test", status="ok")
        write_receipt(doc, IDENTITY, signing_key, mock_storage, sign=False)

        call_kwargs = mock_storage.write_receipt.call_args[1]
        assert call_kwargs["signature"] is None
        parsed = json.loads(call_kwargs["payload_json"])
        assert "proof" not in parsed

    def test_no_signing_key_no_proof(self, mock_storage):
        doc = order_ack(skill_name="test", status="ok")
        write_receipt(doc, IDENTITY, None, mock_storage, sign=True)

        call_kwargs = mock_storage.write_receipt.call_args[1]
        assert call_kwargs["signature"] is None

    def test_storage_failure_emits_event(self, mock_storage, mock_bus):
        mock_storage.write_receipt.side_effect = RuntimeError("disk full")
        doc = order_ack(skill_name="test", status="ok")

        # Should not raise
        rid = write_receipt(doc, IDENTITY, None, mock_storage, bus=mock_bus)
        assert rid == doc["receipt_id"]

        mock_bus.emit.assert_called_once()
        call_args = mock_bus.emit.call_args
        assert call_args[0][0] == "receipt.write_failed"

    def test_storage_failure_no_bus(self, mock_storage):
        mock_storage.write_receipt.side_effect = RuntimeError("disk full")
        doc = order_ack(skill_name="test", status="ok")

        # Should not raise even without bus
        rid = write_receipt(doc, IDENTITY, None, mock_storage)
        assert rid == doc["receipt_id"]


class TestCallSitePatterns:
    """Verify the call site patterns from the migration guide produce valid results."""

    def test_execution_receipt_success_pattern(self, mock_storage, signing_key):
        doc = execution_receipt(
            provider="node_aaa",
            consumer="node_bbb",
            skill_name="llm-chat",
            status="completed",
            execution={
                "started_at": "2026-03-03T10:00:00.000Z",
                "completed_at": "2026-03-03T10:00:01.500Z",
                "duration_ms": 1500,
                "input_hash": "sha256:abc123",
                "output_hash": "sha256:def456",
                "error": None,
            },
            settlement={
                "credit_note_ref": None,
                "amount": 1.5,
                "currency": "credits",
            },
        )
        rid = write_receipt(
            doc, IDENTITY, signing_key, mock_storage,
            counterparty="node_bbb", order_ref="job_123", sign=True,
        )
        assert rid.startswith("exec_")
        assert doc["execution"]["duration_ms"] == 1500
        assert doc["settlement"]["amount"] == 1.5

    def test_execution_receipt_failed_pattern(self, mock_storage, signing_key):
        doc = execution_receipt(
            provider="node_aaa",
            consumer="node_bbb",
            skill_name="llm-chat",
            status="failed",
            execution={
                "duration_ms": 30000,
                "error": "timeout",
            },
            settlement={"credit_note_ref": None, "amount": 0.0, "currency": "credits"},
        )
        rid = write_receipt(
            doc, IDENTITY, signing_key, mock_storage,
            counterparty="node_bbb", order_ref="job_456", sign=True,
        )
        assert rid.startswith("exec_")
        assert doc["status"] == "failed"

    def test_credit_note_pattern(self, mock_storage, signing_key):
        doc = credit_note(
            provider="node_aaa",
            consumer="node_bbb",
            skill_name="llm-chat",
            amount=1.5,
            note_type="debit",
            currency="credits",
            reference="job_123",
        )
        rid = write_receipt(
            doc, IDENTITY, signing_key, mock_storage,
            counterparty="node_bbb", order_ref="job_123", sign=True,
        )
        assert rid.startswith("cn_")
        assert doc["note_type"] == "debit"

    def test_order_ack_accepted_pattern(self, mock_storage):
        doc = order_ack(
            skill_name="llm-chat",
            status="accepted",
            provider="node_aaa",
            consumer="node_bbb",
            queue={"position": 0, "estimated_wait_ms": None},
        )
        rid = write_receipt(doc, IDENTITY, None, mock_storage, order_ref="job_789")
        assert rid.startswith("oack_")
        assert doc["queue"]["position"] == 0

    def test_order_ack_rejected_pattern(self, mock_storage, signing_key):
        doc = order_ack(
            skill_name="llm-chat",
            status="rejected",
            reason="QUEUE_FULL",
        )
        rid = write_receipt(
            doc, IDENTITY, signing_key, mock_storage,
            counterparty="node_bbb", order_ref="job_101", sign=True,
        )
        assert rid.startswith("oack_")
        assert doc["reason"] == "QUEUE_FULL"

    def test_mail_delivery_receipt_pattern(self, mock_storage, signing_key):
        doc = mail_delivery_receipt(
            recipient="node_bbb",
            message_id="msg_001",
            status="delivered",
            delivery={
                "attempt": 1,
                "endpoint": "node_bbb@tcp",
                "duration_ms": 120,
                "ack_item_ids": ["item_1", "item_2"],
            },
        )
        rid = write_receipt(
            doc, IDENTITY, signing_key, mock_storage,
            counterparty="node_bbb", order_ref="msg_001", sign=True,
        )
        assert rid.startswith("mdr_")

    def test_mail_receive_receipt_stored_pattern(self, mock_storage, signing_key):
        doc = mail_receive_receipt(
            sender="node_bbb",
            message_id="msg_001",
            message_type="knarr/user",
            receipt={"status": "stored", "payload_bytes": 256, "payload_hash": "sha256:abc"},
        )
        rid = write_receipt(
            doc, IDENTITY, signing_key, mock_storage,
            counterparty="node_bbb", order_ref="msg_001", sign=True,
        )
        assert rid.startswith("mrr_")
        assert doc["receipt"]["status"] == "stored"

    def test_mail_receive_receipt_duplicate_pattern(self, mock_storage):
        doc = mail_receive_receipt(
            sender="node_bbb",
            message_id="msg_001",
            message_type="knarr/user",
            receipt={"status": "duplicate", "payload_bytes": 0, "payload_hash": None},
        )
        rid = write_receipt(
            doc, IDENTITY, None, mock_storage,
            counterparty="node_bbb", order_ref="msg_001",
        )
        assert rid.startswith("mrr_")
        assert doc["receipt"]["status"] == "duplicate"
