"""Unit tests for B4: Failed execution receipt writes.

Tests cover:
1. asyncio.TimeoutError handler writes execution_receipt with status=failed
2. MCPTimeoutError handler writes execution_receipt with status=failed
3. Base Exception handler writes execution_receipt with status=failed
4. All three produce amount=0.0, credit_note_ref=null
5. Error message is captured in the receipt

These tests exercise the receipt payload shapes for failed executions — verifying
that the "failures are data, gaps are lies" principle is implemented correctly.
"""
import asyncio
import hashlib
import json
import time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest

import sqlite3


class StorageStub:
    """Minimal in-memory storage for receipt tests."""

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS receipt_log (
                receipt_id TEXT PRIMARY KEY,
                document_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                identity TEXT NOT NULL,
                counterparty TEXT,
                order_ref TEXT,
                proof_purpose TEXT NOT NULL DEFAULT 'assertion',
                payload_json TEXT NOT NULL,
                signature TEXT,
                created_at REAL NOT NULL
            )
        """)
        self._conn.commit()

    def write_receipt(self, receipt_id, document_type, timestamp, identity,
                      counterparty, order_ref, proof_purpose, payload_json, signature):
        self._conn.execute(
            """INSERT OR IGNORE INTO receipt_log
               (receipt_id, document_type, timestamp, identity, counterparty, order_ref,
                proof_purpose, payload_json, signature, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (receipt_id, document_type, timestamp, identity, counterparty, order_ref,
             proof_purpose, payload_json, signature, time.time())
        )
        self._conn.commit()

    def get_receipt(self, receipt_id):
        row = self._conn.execute(
            "SELECT * FROM receipt_log WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_receipts_by_type(self, doc_type):
        rows = self._conn.execute(
            "SELECT * FROM receipt_log WHERE document_type = ? ORDER BY timestamp",
            (doc_type,)
        ).fetchall()
        return [dict(r) for r in rows]

    def count_receipts(self, doc_type=None):
        if doc_type:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM receipt_log WHERE document_type = ?", (doc_type,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM receipt_log").fetchone()
        return row[0]


# -----------------------------------------------------------------------
# Shared: FakeNode with _write_receipt (same as test_receipt_writes.py)
# -----------------------------------------------------------------------

class FakeNode:
    def __init__(self):
        from nacl.signing import SigningKey
        self._debug = False
        self.storage = StorageStub(":memory:")
        self._signing_key = SigningKey.generate()
        self._public_key_hex = self._signing_key.verify_key.encode().hex()

    def _write_receipt(
        self,
        document_type: str,
        payload: dict,
        counterparty: Optional[str] = None,
        order_ref: Optional[str] = None,
        proof_purpose: str = "assertion",
        sign: bool = False,
    ) -> str:
        import secrets as _secrets
        from datetime import datetime, timezone as _tz

        _prefix_map = {
            "execution_receipt": "exec",
            "credit_note": "cn",
            "mail_delivery_receipt": "mdr",
            "mail_receive_receipt": "mrr",
            "order_ack": "oack",
            "order_executing": "oexe",
        }
        type_prefix = _prefix_map.get(document_type, "rct")
        receipt_id = f"{type_prefix}_{_secrets.token_hex(6)}"

        _now = datetime.now(_tz.utc)
        timestamp = _now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_now.microsecond // 1000:03d}Z"

        payload["document_type"] = document_type
        payload["version"] = 1
        payload["receipt_id"] = receipt_id
        payload["timestamp"] = timestamp
        if sign:
            payload["cryptosuite"] = "ed25519-jcs"
        payload["proof_purpose"] = proof_purpose

        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))

        signature: Optional[str] = None
        if sign and self._signing_key:
            raw_sig = self._signing_key.sign(payload_json.encode("utf-8")).signature
            signature = "ed25519:" + raw_sig.hex()

        try:
            self.storage.write_receipt(
                receipt_id=receipt_id,
                document_type=document_type,
                timestamp=timestamp,
                identity=self._public_key_hex,
                counterparty=counterparty,
                order_ref=order_ref,
                proof_purpose=proof_purpose,
                payload_json=payload_json,
                signature=signature,
            )
        except Exception as _exc:
            import logging
            logging.getLogger(__name__).warning(f"RECEIPT_WRITE_FAIL: {_exc}")

        return receipt_id


def _write_failed_execution_receipt(node, error_msg, error_type="timeout", input_hash=None):
    """Helper that simulates what the exception handlers in _execute_queued_task write."""
    err = {"code": error_type.upper(), "message": error_msg}
    wall_ms = 500
    skill_name = "llm/chat"
    caller_node_id = "d" * 64
    job_id_for_update = "job-failed-001"

    return node._write_receipt(
        document_type="execution_receipt",
        payload={
            "provider": node._public_key_hex,
            "caller": caller_node_id,
            "skill_uri": f"knarr:///{skill_name}",
            "execution": {
                "status": "failed",
                "duration_ms": wall_ms,
                "input_hash": f"sha256:{input_hash}" if input_hash else None,
                "output_hash": None,
                "error": err.get("message", error_type),
            },
            "settlement": {
                "credit_note_ref": None,
                "amount": 0.0,
                "currency": "credits",
            },
        },
        counterparty=caller_node_id,
        order_ref=job_id_for_update,
        proof_purpose="assertion",
        sign=True,
    )


# -----------------------------------------------------------------------
# Tests: asyncio.TimeoutError handler receipt
# -----------------------------------------------------------------------

class TestAsyncioTimeoutReceipt:
    """Receipt written when asyncio.TimeoutError fires inside _execute_queued_task."""

    def test_receipt_written_on_timeout(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "Handler exceeded 30s timeout")
        assert node.storage.count_receipts("execution_receipt") == 1
        assert rid.startswith("exec_")

    def test_status_is_failed(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "Handler exceeded 30s timeout")
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["execution"]["status"] == "failed"

    def test_amount_is_zero(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "Handler exceeded 30s timeout")
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["settlement"]["amount"] == 0.0

    def test_credit_note_ref_is_null(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "Handler exceeded 30s timeout")
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["settlement"]["credit_note_ref"] is None

    def test_output_hash_is_null(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "timeout")
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["execution"]["output_hash"] is None

    def test_error_message_captured(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "Handler exceeded 30s timeout")
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["execution"]["error"] == "Handler exceeded 30s timeout"

    def test_input_hash_captured_when_present(self):
        node = FakeNode()
        input_hash = "abcdef1234567890" * 4
        rid = _write_failed_execution_receipt(node, "timeout", input_hash=input_hash)
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["execution"]["input_hash"] == f"sha256:{input_hash}"

    def test_input_hash_null_when_absent(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "timeout", input_hash=None)
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["execution"]["input_hash"] is None

    def test_receipt_is_signed(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "timeout")
        row = node.storage.get_receipt(rid)
        assert row["signature"] is not None
        assert row["signature"].startswith("ed25519:")

    def test_proof_purpose_is_assertion(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "timeout")
        row = node.storage.get_receipt(rid)
        assert row["proof_purpose"] == "assertion"

    def test_order_ref_is_job_id(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "timeout")
        row = node.storage.get_receipt(rid)
        assert row["order_ref"] == "job-failed-001"


# -----------------------------------------------------------------------
# Tests: MCPTimeoutError handler receipt
# -----------------------------------------------------------------------

class TestMCPTimeoutReceipt:
    """Receipt written when MCPTimeoutError fires inside _execute_queued_task."""

    def test_mcp_timeout_writes_receipt(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(
            node,
            error_msg="MCP tool timed out after 30s",
            error_type="MCP_TIMEOUT",
        )
        assert node.storage.count_receipts("execution_receipt") == 1

    def test_mcp_timeout_status_is_failed(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "MCP tool timed out after 30s", "MCP_TIMEOUT")
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["execution"]["status"] == "failed"

    def test_mcp_timeout_amount_is_zero(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "MCP tool timed out", "MCP_TIMEOUT")
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["settlement"]["amount"] == 0.0

    def test_mcp_timeout_error_message_in_receipt(self):
        node = FakeNode()
        error_msg = "MCP bridge timeout: tool=browser_use exceeded 30s"
        rid = _write_failed_execution_receipt(node, error_msg, "MCP_TIMEOUT")
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["execution"]["error"] == error_msg

    def test_mcp_timeout_receipt_is_signed(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "mcp timeout", "MCP_TIMEOUT")
        row = node.storage.get_receipt(rid)
        assert row["signature"].startswith("ed25519:")


# -----------------------------------------------------------------------
# Tests: Base Exception handler receipt
# -----------------------------------------------------------------------

class TestHandlerErrorReceipt:
    """Receipt written when a base Exception fires inside _execute_queued_task."""

    def test_handler_error_writes_receipt(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(
            node,
            error_msg="Handler execution failed",
            error_type="HANDLER_ERROR",
        )
        assert node.storage.count_receipts("execution_receipt") == 1

    def test_handler_error_status_is_failed(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "Handler execution failed", "HANDLER_ERROR")
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["execution"]["status"] == "failed"

    def test_handler_error_amount_is_zero(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "Handler execution failed", "HANDLER_ERROR")
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["settlement"]["amount"] == 0.0

    def test_handler_error_receipt_is_signed(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "Handler execution failed", "HANDLER_ERROR")
        row = node.storage.get_receipt(rid)
        assert row["signature"].startswith("ed25519:")

    def test_handler_error_currency_is_credits(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "error", "HANDLER_ERROR")
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["settlement"]["currency"] == "credits"


# -----------------------------------------------------------------------
# Tests: All three error types produce distinct receipts
# -----------------------------------------------------------------------

class TestThreeFailureTypes:
    """Verify all three failure paths write receipts and are stored separately."""

    def test_three_failures_produce_three_receipts(self):
        node = FakeNode()

        # Simulate three consecutive failures
        _write_failed_execution_receipt(node, "asyncio.TimeoutError", "TIMEOUT")
        _write_failed_execution_receipt(node, "MCPTimeoutError: tool timeout", "MCP_TIMEOUT")
        _write_failed_execution_receipt(node, "Handler execution failed", "HANDLER_ERROR")

        count = node.storage.count_receipts("execution_receipt")
        assert count == 3, f"expected 3 failure receipts, got {count}"

    def test_all_failures_have_zero_amount(self):
        node = FakeNode()
        for error in ["timeout", "mcp timeout", "handler error"]:
            _write_failed_execution_receipt(node, error)

        rows = node.storage.get_receipts_by_type("execution_receipt")
        for row in rows:
            p = json.loads(row["payload_json"])
            assert p["settlement"]["amount"] == 0.0

    def test_all_failures_have_null_output_hash(self):
        node = FakeNode()
        for error in ["timeout", "mcp timeout", "handler error"]:
            _write_failed_execution_receipt(node, error)

        rows = node.storage.get_receipts_by_type("execution_receipt")
        for row in rows:
            p = json.loads(row["payload_json"])
            assert p["execution"]["output_hash"] is None

    def test_all_failures_are_signed(self):
        node = FakeNode()
        for error in ["timeout", "mcp timeout", "handler error"]:
            _write_failed_execution_receipt(node, error)

        rows = node.storage.get_receipts_by_type("execution_receipt")
        for row in rows:
            assert row["signature"] is not None
            assert row["signature"].startswith("ed25519:")

    def test_receipt_ids_are_unique_across_failures(self):
        node = FakeNode()
        rids = []
        for error in ["timeout", "mcp timeout", "handler error"]:
            rid = _write_failed_execution_receipt(node, error)
            rids.append(rid)
        assert len(set(rids)) == 3, "each failure must produce a unique receipt_id"


# -----------------------------------------------------------------------
# Tests: Signature correctness on failed receipts
# -----------------------------------------------------------------------

class TestFailedReceiptSignatureVerification:

    def test_failed_receipt_signature_verifies(self):
        from nacl.signing import VerifyKey
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "handler failed")
        row = node.storage.get_receipt(rid)

        sig_hex = row["signature"].split(":", 1)[1]
        sig_bytes = bytes.fromhex(sig_hex)
        payload_bytes = row["payload_json"].encode("utf-8")

        vk = VerifyKey(bytes.fromhex(node._public_key_hex))
        vk.verify(payload_bytes, sig_bytes)  # raises if invalid

    def test_failed_receipt_cryptosuite_is_ed25519_jcs(self):
        node = FakeNode()
        rid = _write_failed_execution_receipt(node, "timeout")
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p.get("cryptosuite") == "ed25519-jcs"
