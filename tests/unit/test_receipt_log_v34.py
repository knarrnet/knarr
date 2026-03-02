"""Tests for B1: receipt_log table + write_receipt() helper."""
import pytest
import json
import time
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from knarr.dht.storage import Storage


class TestReceiptLog:
    """Tests for receipt_log table and write_receipt() method."""

    def setup_method(self):
        """Create fresh in-memory storage for each test."""
        self.storage = Storage(":memory:")

    def test_write_receipt_basic(self):
        """Test writing a receipt and reading it back."""
        receipt_id = "test_receipt_001"
        document_type = "mail_delivery_receipt"
        timestamp = "2026-03-02T12:00:00Z"
        identity = "a" * 64
        counterparty = "b" * 64
        order_ref = "order_123"
        proof_purpose = "assertion"
        payload = {"status": "ack", "attempt": 1}
        payload_json = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        signature = "sig_hex_abc123"

        self.storage.write_receipt(
            receipt_id=receipt_id,
            document_type=document_type,
            timestamp=timestamp,
            identity=identity,
            counterparty=counterparty,
            order_ref=order_ref,
            proof_purpose=proof_purpose,
            payload_json=payload_json,
            signature=signature
        )

        # Read back from database
        conn = self.storage._get_conn()
        cursor = conn.execute(
            "SELECT * FROM receipt_log WHERE receipt_id = ?", (receipt_id,)
        )
        row = cursor.fetchone()
        
        assert row is not None
        assert row[0] == receipt_id
        assert row[1] == document_type
        assert row[2] == timestamp
        assert row[3] == identity
        assert row[4] == counterparty
        assert row[5] == order_ref
        assert row[6] == proof_purpose
        assert json.loads(row[7]) == payload
        assert row[8] == signature

    def test_write_receipt_duplicate_ignored(self):
        """Test that duplicate receipt_id is silently ignored (idempotent)."""
        receipt_id = "test_receipt_dup"
        payload_json = json.dumps({"test": "data"})

        # Write first receipt
        self.storage.write_receipt(
            receipt_id=receipt_id,
            document_type="mail_delivery_receipt",
            timestamp="2026-03-02T12:00:00Z",
            identity="a" * 64,
            counterparty="b" * 64,
            order_ref=None,
            proof_purpose="assertion",
            payload_json=payload_json,
            signature="sig1"
        )

        # Write duplicate with different data
        self.storage.write_receipt(
            receipt_id=receipt_id,
            document_type="mail_delivery_receipt",
            timestamp="2026-03-02T13:00:00Z",
            identity="c" * 64,
            counterparty="d" * 64,
            order_ref=None,
            proof_purpose="assertion",
            payload_json=json.dumps({"test": "different"}),
            signature="sig2"
        )

        # Should only have one row
        conn = self.storage._get_conn()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM receipt_log WHERE receipt_id = ?", (receipt_id,)
        )
        count = cursor.fetchone()[0]
        assert count == 1

        # Original data should be preserved
        cursor = conn.execute(
            "SELECT payload_json FROM receipt_log WHERE receipt_id = ?", (receipt_id,)
        )
        row = cursor.fetchone()
        assert json.loads(row[0]) == {"test": "data"}

    def test_write_receipt_null_fields(self):
        """Test writing receipt with optional null fields."""
        self.storage.write_receipt(
            receipt_id="test_nulls",
            document_type="execution_receipt",
            timestamp="2026-03-02T12:00:00Z",
            identity="a" * 64,
            counterparty=None,
            order_ref=None,
            proof_purpose="assertion",
            payload_json="{}",
            signature=None
        )

        conn = self.storage._get_conn()
        cursor = conn.execute(
            "SELECT counterparty, order_ref, signature FROM receipt_log WHERE receipt_id = ?",
            ("test_nulls",)
        )
        row = cursor.fetchone()
        assert row[0] is None
        assert row[1] is None
        assert row[2] is None

    def test_receipt_log_indexes_exist(self):
        """Test that required indexes are created."""
        conn = self.storage._get_conn()
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='receipt_log'"
        )
        indexes = {row[0] for row in cursor.fetchall()}
        
        assert "idx_receipt_log_type_ts" in indexes
        assert "idx_receipt_log_order" in indexes
        assert "idx_receipt_log_identity" in indexes
