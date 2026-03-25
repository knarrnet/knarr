"""Tests for v0.35.0 PluginContext bridge (sign_document + query_receipts)."""

import json
import sqlite3
import time
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from nacl.signing import SigningKey

from knarr.commerce.plugin_bridge import (
    make_sign_callback,
    make_query_receipts_callback,
    query_receipts,
)
from knarr.core.proof import verify_document


# ── Helpers ───────────────────────────────────────────────────────────


class MockStorage:
    """In-memory receipt_log for testing."""

    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute("""
            CREATE TABLE receipt_log (
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

    def _get_conn(self):
        return self._conn

    def query_receipts_filtered(self, document_type=None, counterparty=None,
                                 since=None, limit=50):
        """Mirror Storage.query_receipts_filtered for test mock."""
        conn = self._conn
        clauses = []
        params = []
        if document_type:
            clauses.append("document_type = ?")
            params.append(document_type)
        if counterparty:
            clauses.append("counterparty = ?")
            params.append(counterparty)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        where = " AND ".join(clauses) if clauses else "1=1"
        sql = (
            "SELECT receipt_id, document_type, timestamp, identity, counterparty, "
            "order_ref, proof_purpose, payload_json, signature, created_at "
            f"FROM receipt_log WHERE {where} ORDER BY created_at DESC LIMIT ?"
        )
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            payload = {}
            try:
                payload = json.loads(row[7]) if row[7] else {}
            except (json.JSONDecodeError, TypeError):
                pass
            results.append({
                "receipt_id": row[0],
                "document_type": row[1],
                "timestamp": row[2],
                "identity": row[3],
                "counterparty": row[4],
                "order_ref": row[5],
                "proof_purpose": row[6],
                "payload_json": row[7],
                "payload": payload,
                "signature": row[8],
                "created_at": row[9],
            })
        return results

    def insert(self, receipt_id, document_type, counterparty=None,
               payload=None, created_at=None):
        now = created_at or time.time()
        self._conn.execute(
            """INSERT INTO receipt_log
               (receipt_id, document_type, timestamp, identity, counterparty,
                order_ref, proof_purpose, payload_json, signature, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (receipt_id, document_type, "2026-03-03T00:00:00.000Z",
             "my_node", counterparty, None, "assertionMethod",
             json.dumps(payload or {}), None, now),
        )
        self._conn.commit()


# ── sign_document callback ────────────────────────────────────────────


class TestSignCallback:
    def setup_method(self):
        self.sk = SigningKey.generate()
        self.vk = self.sk.verify_key
        self.node_id = "abc123def456"

    def test_basic_sign(self):
        sign = make_sign_callback(self.sk, self.node_id)
        doc = {"skill_name": "echo", "status": "completed"}
        secured = sign(doc)
        assert "proof" in secured
        assert secured["proof"]["proofValue"].startswith("z")

    def test_sign_verifies(self):
        sign = make_sign_callback(self.sk, self.node_id)
        doc = {"amount": 1.5, "type": "credit_note"}
        secured = sign(doc)
        assert verify_document(secured, self.vk)

    def test_sign_does_not_mutate_input(self):
        sign = make_sign_callback(self.sk, self.node_id)
        doc = {"field": "value"}
        original = dict(doc)
        sign(doc)
        assert doc == original

    def test_verification_method_uses_full_node_id(self):
        sign = make_sign_callback(self.sk, self.node_id)
        secured = sign({"test": True})
        assert secured["proof"]["verificationMethod"] == f"did:knarr:{self.node_id}#key-1"

    def test_custom_proof_purpose(self):
        sign = make_sign_callback(self.sk, self.node_id)
        secured = sign({"test": True}, proof_purpose="authentication")
        assert secured["proof"]["proofPurpose"] == "authentication"


# ── query_receipts callback ───────────────────────────────────────────


class TestQueryReceipts:
    def setup_method(self):
        self.storage = MockStorage()
        self.storage.insert("exec_001", "execution_receipt",
                           counterparty="peer_a", payload={"status": "completed"},
                           created_at=1000.0)
        self.storage.insert("cn_001", "credit_note",
                           counterparty="peer_a", payload={"amount": 5.0},
                           created_at=2000.0)
        self.storage.insert("exec_002", "execution_receipt",
                           counterparty="peer_b", payload={"status": "failed"},
                           created_at=3000.0)
        self.storage.insert("adm_001", "admission_decision",
                           counterparty="peer_b", payload={"outcome": "hard_block"},
                           created_at=4000.0)

    def test_query_all(self):
        results = query_receipts(self.storage)
        assert len(results) == 4

    def test_query_by_document_type(self):
        results = query_receipts(self.storage, document_type="execution_receipt")
        assert len(results) == 2
        assert all(r["document_type"] == "execution_receipt" for r in results)

    def test_query_by_counterparty(self):
        results = query_receipts(self.storage, counterparty="peer_a")
        assert len(results) == 2
        assert all(r["counterparty"] == "peer_a" for r in results)

    def test_query_by_since(self):
        results = query_receipts(self.storage, since=2500.0)
        assert len(results) == 2  # exec_002 and adm_001
        assert results[0]["receipt_id"] == "adm_001"  # most recent first

    def test_query_combined_filters(self):
        results = query_receipts(
            self.storage,
            document_type="execution_receipt",
            counterparty="peer_b",
        )
        assert len(results) == 1
        assert results[0]["receipt_id"] == "exec_002"

    def test_query_limit(self):
        results = query_receipts(self.storage, limit=2)
        assert len(results) == 2

    def test_query_returns_parsed_payload(self):
        results = query_receipts(self.storage, document_type="credit_note")
        assert results[0]["payload"]["amount"] == 5.0

    def test_query_no_results(self):
        results = query_receipts(self.storage, document_type="nonexistent")
        assert results == []

    def test_callback_wrapper(self):
        cb = make_query_receipts_callback(self.storage)
        results = cb(document_type="admission_decision")
        assert len(results) == 1
        assert results[0]["payload"]["outcome"] == "hard_block"

    def test_order_is_newest_first(self):
        results = query_receipts(self.storage)
        created_times = [r["created_at"] for r in results]
        assert created_times == sorted(created_times, reverse=True)
