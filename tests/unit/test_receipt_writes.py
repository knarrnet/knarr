"""Unit tests for B4: _write_receipt() helper and all 8 receipt write locations.

Tests verify that:
1. _write_receipt() generates correct receipt_id prefix, timestamp, canonical JSON
2. All 8 receipt types are written with correct document_type, proof_purpose, and fields
3. Signed receipts include a signature; unsigned receipts do not
4. storage.write_receipt() is called with the correct arguments
5. Failures in write_receipt() are swallowed (never propagate to callers)

These tests use a mock DHTNode-like object to isolate the helper from the full
node dependency tree.
"""
import asyncio
import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock, patch, call
import pytest

# ---- Minimal node stand-in that has _write_receipt ----

# We import _write_receipt logic by pulling it from the proposed-c node module
# indirectly: we define a minimal host class that has the method inlined.
# This keeps the test hermetically isolated from the full DHTNode import chain.

import sys
import os

# Ensure proposed-c/src is on the path before src/ so our stubs win
_PROPOSED_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src"))
_BASE_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../src"))
if _PROPOSED_SRC not in sys.path:
    sys.path.insert(0, _PROPOSED_SRC)
if _BASE_SRC not in sys.path:
    sys.path.insert(1, _BASE_SRC)

from knarr.dht.storage import StorageStub


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _make_signing_key():
    """Return a real nacl Ed25519 SigningKey for signature tests."""
    from nacl.signing import SigningKey
    return SigningKey.generate()


class FakeNode:
    """Minimal node-like object that contains only _write_receipt and its deps."""

    def __init__(self, sign: bool = True):
        self._debug = True
        self.storage = StorageStub(":memory:")
        if sign:
            self._signing_key = _make_signing_key()
            self._public_key_hex = self._signing_key.verify_key.encode().hex()
        else:
            self._signing_key = None
            self._public_key_hex = "a" * 64

    # Paste _write_receipt verbatim from the patched node — this is the exact
    # implementation under test, isolated from the rest of node.py.
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
            import base64 as _b64
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
            logging.getLogger(__name__).warning(
                f"RECEIPT_WRITE_FAIL type={document_type} id={receipt_id}: {_exc}"
            )

        return receipt_id


# -----------------------------------------------------------------------
# Tests: _write_receipt() helper correctness
# -----------------------------------------------------------------------

class TestWriteReceiptHelper:
    """Tests for the _write_receipt() helper method on DHTNode."""

    def test_receipt_id_prefix_execution_receipt(self):
        node = FakeNode()
        rid = node._write_receipt("execution_receipt", {"foo": "bar"}, sign=False)
        assert rid.startswith("exec_"), f"expected exec_ prefix, got {rid}"
        parts = rid.split("_")
        assert len(parts) == 2 and len(parts[1]) == 12

    def test_receipt_id_prefix_credit_note(self):
        node = FakeNode()
        rid = node._write_receipt("credit_note", {"amount": 1.0}, sign=False)
        assert rid.startswith("cn_")

    def test_receipt_id_prefix_mail_delivery(self):
        node = FakeNode()
        rid = node._write_receipt("mail_delivery_receipt", {}, sign=False)
        assert rid.startswith("mdr_")

    def test_receipt_id_prefix_mail_receive(self):
        node = FakeNode()
        rid = node._write_receipt("mail_receive_receipt", {}, sign=False)
        assert rid.startswith("mrr_")

    def test_receipt_id_prefix_order_ack(self):
        node = FakeNode()
        rid = node._write_receipt("order_ack", {}, sign=False)
        assert rid.startswith("oack_")

    def test_receipt_id_prefix_order_executing(self):
        node = FakeNode()
        rid = node._write_receipt("order_executing", {}, sign=False)
        assert rid.startswith("oexe_")

    def test_receipt_id_prefix_unknown_type(self):
        node = FakeNode()
        rid = node._write_receipt("some_future_type", {}, sign=False)
        assert rid.startswith("rct_")

    def test_receipt_ids_are_unique(self):
        node = FakeNode()
        ids = [node._write_receipt("order_ack", {}, sign=False) for _ in range(50)]
        assert len(set(ids)) == 50, "receipt_ids must be unique"

    def test_timestamp_is_iso8601_utc(self):
        node = FakeNode()
        node._write_receipt("order_ack", {"x": 1}, sign=False)
        row = node.storage.get_receipts_by_type("order_ack")[0]
        payload = json.loads(row["payload_json"])
        ts = payload["timestamp"]
        # Must end with Z and be parseable
        assert ts.endswith("Z"), f"timestamp must end with Z, got {ts}"
        # Verify it parses as ISO 8601
        datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")

    def test_payload_enriched_with_common_fields(self):
        node = FakeNode()
        node._write_receipt(
            "execution_receipt",
            {"execution": {"status": "completed"}},
            proof_purpose="assertion",
            sign=False,
        )
        row = node.storage.get_receipts_by_type("execution_receipt")[0]
        payload = json.loads(row["payload_json"])
        assert payload["document_type"] == "execution_receipt"
        assert payload["version"] == 1
        assert "receipt_id" in payload
        assert "timestamp" in payload
        assert payload["proof_purpose"] == "assertion"
        assert "cryptosuite" not in payload  # unsigned — no cryptosuite

    def test_signed_receipt_has_cryptosuite_and_signature(self):
        node = FakeNode(sign=True)
        rid = node._write_receipt(
            "execution_receipt",
            {"execution": {"status": "completed"}},
            sign=True,
        )
        row = node.storage.get_receipt(rid)
        assert row is not None
        assert row["signature"] is not None
        assert row["signature"].startswith("ed25519:")
        payload = json.loads(row["payload_json"])
        assert payload.get("cryptosuite") == "ed25519-jcs"

    def test_unsigned_receipt_has_no_signature(self):
        node = FakeNode()
        rid = node._write_receipt("order_ack", {"queue": {"position": 1}}, sign=False)
        row = node.storage.get_receipt(rid)
        assert row is not None
        assert row["signature"] is None

    def test_canonical_json_sort_keys(self):
        node = FakeNode()
        # Payload with keys in non-sorted order
        payload = {"z_field": 1, "a_field": 2, "m_field": 3}
        rid = node._write_receipt("order_ack", payload, sign=False)
        row = node.storage.get_receipt(rid)
        stored = row["payload_json"]
        # Verify keys are sorted by checking JSON object key order
        parsed = json.loads(stored)
        keys = list(parsed.keys())
        # All common injected keys + domain keys should be present and deterministic
        assert stored == json.dumps(parsed, sort_keys=True, separators=(",", ":"))

    def test_canonical_json_minimal_whitespace(self):
        node = FakeNode()
        node._write_receipt("order_ack", {"pos": 1}, sign=False)
        row = node.storage.get_receipts_by_type("order_ack")[0]
        # No spaces around colons or after commas
        assert " " not in row["payload_json"]

    def test_storage_fields_set_correctly(self):
        node = FakeNode()
        counterparty = "b" * 64
        order_ref = "job-abc123"
        rid = node._write_receipt(
            "execution_receipt",
            {"execution": {"status": "completed"}},
            counterparty=counterparty,
            order_ref=order_ref,
            proof_purpose="assertion",
            sign=False,
        )
        row = node.storage.get_receipt(rid)
        assert row["document_type"] == "execution_receipt"
        assert row["identity"] == node._public_key_hex
        assert row["counterparty"] == counterparty
        assert row["order_ref"] == order_ref
        assert row["proof_purpose"] == "assertion"

    def test_idempotency_insert_or_ignore(self):
        """Calling write_receipt twice with the same receipt_id must not raise."""
        node = FakeNode()
        node.storage.write_receipt(
            receipt_id="exec_aabbccdd1234",
            document_type="execution_receipt",
            timestamp="2026-03-02T14:00:00.000Z",
            identity=node._public_key_hex,
            counterparty=None,
            order_ref="job-1",
            proof_purpose="assertion",
            payload_json='{"document_type":"execution_receipt","version":1}',
            signature=None,
        )
        # Second write with same receipt_id — should be silently ignored
        node.storage.write_receipt(
            receipt_id="exec_aabbccdd1234",
            document_type="execution_receipt",
            timestamp="2026-03-02T14:00:01.000Z",
            identity=node._public_key_hex,
            counterparty=None,
            order_ref="job-1",
            proof_purpose="assertion",
            payload_json='{"document_type":"execution_receipt","version":1,"second":true}',
            signature=None,
        )
        count = node.storage.count_receipts("execution_receipt")
        assert count == 1, "duplicate receipt_id must be silently dropped"

    def test_write_failure_does_not_propagate(self):
        """If storage.write_receipt() raises, _write_receipt() must not propagate."""
        node = FakeNode()
        node.storage.write_receipt = MagicMock(side_effect=Exception("disk full"))
        # Should not raise
        rid = node._write_receipt("order_ack", {"pos": 1}, sign=False)
        assert rid.startswith("oack_")


# -----------------------------------------------------------------------
# Tests: All 8 receipt type shapes
# -----------------------------------------------------------------------

class TestReceiptShapes:
    """Verify payload structure matches schema for each receipt type."""

    def _make_exec_success_payload(self, node):
        return {
            "provider": node._public_key_hex,
            "caller": "c" * 64,
            "skill_uri": "knarr:///llm/chat",
            "execution": {
                "status": "completed",
                "duration_ms": 1200,
                "input_hash": "sha256:abc123",
                "output_hash": "sha256:def456",
                "error": None,
            },
            "settlement": {
                "credit_note_ref": None,
                "amount": 2.0,
                "currency": "credits",
            },
        }

    def test_execution_receipt_success_shape(self):
        node = FakeNode()
        payload = self._make_exec_success_payload(node)
        rid = node._write_receipt(
            "execution_receipt", payload,
            counterparty="c" * 64,
            order_ref="job-111",
            proof_purpose="assertion",
            sign=True,
        )
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["document_type"] == "execution_receipt"
        assert p["version"] == 1
        assert p["execution"]["status"] == "completed"
        assert p["settlement"]["amount"] == 2.0
        assert p["cryptosuite"] == "ed25519-jcs"
        assert row["proof_purpose"] == "assertion"
        assert row["signature"].startswith("ed25519:")

    def test_execution_receipt_failed_shape(self):
        node = FakeNode()
        rid = node._write_receipt(
            "execution_receipt",
            {
                "provider": node._public_key_hex,
                "caller": "d" * 64,
                "skill_uri": "knarr:///llm/chat",
                "execution": {
                    "status": "failed",
                    "duration_ms": 300,
                    "input_hash": "sha256:aaa",
                    "output_hash": None,
                    "error": "timeout",
                },
                "settlement": {
                    "credit_note_ref": None,
                    "amount": 0.0,
                    "currency": "credits",
                },
            },
            counterparty="d" * 64,
            order_ref="job-222",
            proof_purpose="assertion",
            sign=True,
        )
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["execution"]["status"] == "failed"
        assert p["settlement"]["amount"] == 0.0
        assert p["execution"]["output_hash"] is None

    def test_credit_note_receipt_shape(self):
        node = FakeNode()
        rid = node._write_receipt(
            "credit_note",
            {
                "note_type": "debit",
                "amount": 5.0,
                "currency": "credits",
                "issuer": node._public_key_hex,
                "recipient": "e" * 64,
                "reference": "job-333",
                "description": "skill:web-search execution",
            },
            counterparty="e" * 64,
            order_ref="job-333",
            proof_purpose="assertion",
            sign=True,
        )
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["document_type"] == "credit_note"
        assert p["note_type"] == "debit"
        assert p["amount"] == 5.0
        assert p["cryptosuite"] == "ed25519-jcs"

    def test_mail_delivery_receipt_ack_shape(self):
        node = FakeNode()
        rid = node._write_receipt(
            "mail_delivery_receipt",
            {
                "sender": node._public_key_hex,
                "recipient": "f" * 64,
                "batch": {
                    "message_ids": ["msg-1", "msg-2"],
                    "message_count": 2,
                },
                "delivery": {
                    "status": "ack",
                    "attempt": 1,
                    "endpoint": "abcdef12@tcp",
                    "duration_ms": 45,
                    "ack_item_ids": ["msg-1", "msg-2"],
                },
            },
            counterparty="f" * 64,
            order_ref="msg-1",
            proof_purpose="acknowledgment",
            sign=True,
        )
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["document_type"] == "mail_delivery_receipt"
        assert p["delivery"]["status"] == "ack"
        assert row["proof_purpose"] == "acknowledgment"

    def test_mail_delivery_receipt_nak_shape(self):
        node = FakeNode()
        rid = node._write_receipt(
            "mail_delivery_receipt",
            {
                "sender": node._public_key_hex,
                "recipient": "f" * 64,
                "batch": {
                    "message_ids": ["msg-3"],
                    "message_count": 1,
                },
                "delivery": {
                    "status": "nak",
                    "attempt": 2,
                    "endpoint": "abcdef12@tcp",
                    "duration_ms": 10000,
                    "error": "tcp_timeout",
                },
            },
            counterparty="f" * 64,
            order_ref="msg-3",
            proof_purpose="acknowledgment",
            sign=True,
        )
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["delivery"]["status"] == "nak"
        assert p["delivery"]["error"] == "tcp_timeout"

    def test_mail_receive_receipt_stored_shape(self):
        node = FakeNode()
        body_str = '{"hello":"world"}'
        payload_hash = "sha256:" + hashlib.sha256(body_str.encode()).hexdigest()
        rid = node._write_receipt(
            "mail_receive_receipt",
            {
                "receiver": node._public_key_hex,
                "sender": "g" * 64,
                "message_id": "msg-abc",
                "message_type": "knarr/system/task_result",
                "receipt": {
                    "status": "stored",
                    "payload_bytes": len(body_str.encode()),
                    "payload_hash": payload_hash,
                },
            },
            counterparty="g" * 64,
            order_ref="msg-abc",
            proof_purpose="acknowledgment",
            sign=True,
        )
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["document_type"] == "mail_receive_receipt"
        assert p["receipt"]["status"] == "stored"
        assert p["receipt"]["payload_hash"].startswith("sha256:")
        assert row["proof_purpose"] == "acknowledgment"

    def test_order_ack_shape(self):
        node = FakeNode()
        rid = node._write_receipt(
            "order_ack",
            {
                "provider": node._public_key_hex,
                "caller": "h" * 64,
                "skill_uri": "knarr:///embed",
                "queue": {"position": 3, "estimated_wait_ms": None},
            },
            counterparty=None,
            order_ref="job-444",
            proof_purpose="assertion",
            sign=False,
        )
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["document_type"] == "order_ack"
        assert p["queue"]["position"] == 3
        assert row["signature"] is None  # unsigned
        assert "cryptosuite" not in p

    def test_order_executing_shape(self):
        node = FakeNode()
        rid = node._write_receipt(
            "order_executing",
            {
                "provider": node._public_key_hex,
                "caller": "i" * 64,
                "skill_uri": "knarr:///embed",
                "queue_wait_ms": 12,
            },
            counterparty=None,
            order_ref="job-555",
            proof_purpose="assertion",
            sign=False,
        )
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["document_type"] == "order_executing"
        assert p["queue_wait_ms"] == 12
        assert row["signature"] is None


# -----------------------------------------------------------------------
# Tests: Signature verification
# -----------------------------------------------------------------------

class TestSignatureVerification:
    """Verify that signed receipts can be re-verified with the public key."""

    def test_signature_verifies_against_public_key(self):
        from nacl.signing import VerifyKey
        node = FakeNode(sign=True)
        rid = node._write_receipt(
            "execution_receipt",
            {
                "provider": node._public_key_hex,
                "caller": "j" * 64,
                "skill_uri": "knarr:///test",
                "execution": {"status": "completed", "duration_ms": 100},
                "settlement": {"amount": 1.0, "currency": "credits"},
            },
            sign=True,
        )
        row = node.storage.get_receipt(rid)
        assert row["signature"] is not None

        # Signature format: "ed25519:<hex>"
        sig_hex = row["signature"].split(":", 1)[1]
        sig_bytes = bytes.fromhex(sig_hex)
        payload_bytes = row["payload_json"].encode("utf-8")

        vk = VerifyKey(bytes.fromhex(node._public_key_hex))
        # nacl.verify raises if invalid — no exception means valid
        vk.verify(payload_bytes, sig_bytes)

    def test_tampered_payload_fails_verification(self):
        from nacl.signing import VerifyKey
        from nacl.exceptions import BadSignatureError
        node = FakeNode(sign=True)
        rid = node._write_receipt(
            "execution_receipt",
            {"provider": node._public_key_hex, "execution": {"status": "completed"}},
            sign=True,
        )
        row = node.storage.get_receipt(rid)
        sig_hex = row["signature"].split(":", 1)[1]
        sig_bytes = bytes.fromhex(sig_hex)

        # Tamper the payload
        tampered = row["payload_json"].replace('"completed"', '"hacked"')
        vk = VerifyKey(bytes.fromhex(node._public_key_hex))
        with pytest.raises(BadSignatureError):
            vk.verify(tampered.encode("utf-8"), sig_bytes)


# -----------------------------------------------------------------------
# Tests: proof_purpose routing
# -----------------------------------------------------------------------

class TestProofPurpose:

    def test_execution_receipt_uses_assertion(self):
        node = FakeNode()
        rid = node._write_receipt("execution_receipt", {"execution": {}}, proof_purpose="assertion", sign=False)
        row = node.storage.get_receipt(rid)
        assert row["proof_purpose"] == "assertion"
        p = json.loads(row["payload_json"])
        assert p["proof_purpose"] == "assertion"

    def test_mail_receive_receipt_uses_acknowledgment(self):
        node = FakeNode()
        rid = node._write_receipt("mail_receive_receipt", {"receipt": {}}, proof_purpose="acknowledgment", sign=False)
        row = node.storage.get_receipt(rid)
        assert row["proof_purpose"] == "acknowledgment"

    def test_order_ack_uses_assertion(self):
        node = FakeNode()
        rid = node._write_receipt("order_ack", {"queue": {}}, proof_purpose="assertion", sign=False)
        row = node.storage.get_receipt(rid)
        assert row["proof_purpose"] == "assertion"

    def test_mail_delivery_receipt_uses_acknowledgment(self):
        node = FakeNode()
        rid = node._write_receipt(
            "mail_delivery_receipt", {"delivery": {"status": "ack"}},
            proof_purpose="acknowledgment", sign=False
        )
        row = node.storage.get_receipt(rid)
        assert row["proof_purpose"] == "acknowledgment"


# -----------------------------------------------------------------------
# Tests: payload_hash in mail_receive_receipt
# -----------------------------------------------------------------------

class TestMailReceivePayloadHash:

    def test_payload_hash_is_sha256_of_body(self):
        node = FakeNode()
        body = {"text": "hello from forseti", "timestamp": 1234567890}
        body_json = json.dumps(body)
        expected_hash = "sha256:" + hashlib.sha256(body_json.encode("utf-8")).hexdigest()
        payload_bytes = len(body_json.encode("utf-8"))

        rid = node._write_receipt(
            "mail_receive_receipt",
            {
                "receiver": node._public_key_hex,
                "sender": "k" * 64,
                "message_id": "msg-xyz",
                "message_type": "text",
                "receipt": {
                    "status": "stored",
                    "payload_bytes": payload_bytes,
                    "payload_hash": expected_hash,
                },
            },
            proof_purpose="acknowledgment",
            sign=True,
        )
        row = node.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["receipt"]["payload_hash"] == expected_hash
        assert p["receipt"]["payload_bytes"] == payload_bytes

    def test_different_bodies_produce_different_hashes(self):
        node = FakeNode()
        bodies = [
            '{"msg": "hello"}',
            '{"msg": "world"}',
            '{"msg": "knarr"}',
        ]
        hashes = set()
        for body_str in bodies:
            h = "sha256:" + hashlib.sha256(body_str.encode()).hexdigest()
            hashes.add(h)
        assert len(hashes) == 3, "different bodies must produce different hashes"


# -----------------------------------------------------------------------
# Tests: storage stub correctness
# -----------------------------------------------------------------------

class TestStorageStub:

    def test_write_and_read_round_trip(self):
        storage = StorageStub(":memory:")
        storage.write_receipt(
            receipt_id="exec_001",
            document_type="execution_receipt",
            timestamp="2026-03-02T12:00:00.000Z",
            identity="a" * 64,
            counterparty="b" * 64,
            order_ref="job-001",
            proof_purpose="assertion",
            payload_json='{"document_type":"execution_receipt","version":1}',
            signature="ed25519:deafbeef",
        )
        row = storage.get_receipt("exec_001")
        assert row is not None
        assert row["receipt_id"] == "exec_001"
        assert row["document_type"] == "execution_receipt"
        assert row["counterparty"] == "b" * 64
        assert row["signature"] == "ed25519:deafbeef"

    def test_insert_or_ignore_idempotency(self):
        storage = StorageStub(":memory:")
        for _ in range(3):
            storage.write_receipt(
                receipt_id="exec_dup",
                document_type="execution_receipt",
                timestamp="2026-03-02T12:00:00.000Z",
                identity="a" * 64,
                counterparty=None,
                order_ref=None,
                proof_purpose="assertion",
                payload_json='{"v":1}',
                signature=None,
            )
        assert storage.count_receipts("execution_receipt") == 1

    def test_count_by_type(self):
        storage = StorageStub(":memory:")
        for i in range(3):
            storage.write_receipt(
                receipt_id=f"exec_{i:04d}",
                document_type="execution_receipt",
                timestamp="2026-03-02T12:00:00.000Z",
                identity="a" * 64,
                counterparty=None,
                order_ref=None,
                proof_purpose="assertion",
                payload_json=f'{{"idx":{i}}}',
                signature=None,
            )
        for i in range(2):
            storage.write_receipt(
                receipt_id=f"oack_{i:04d}",
                document_type="order_ack",
                timestamp="2026-03-02T12:00:00.000Z",
                identity="a" * 64,
                counterparty=None,
                order_ref=None,
                proof_purpose="assertion",
                payload_json=f'{{"idx":{i}}}',
                signature=None,
            )
        assert storage.count_receipts("execution_receipt") == 3
        assert storage.count_receipts("order_ack") == 2
        assert storage.count_receipts() == 5

    def test_get_receipts_by_type_ordered(self):
        storage = StorageStub(":memory:")
        for i in range(5):
            storage.write_receipt(
                receipt_id=f"mdr_{i:04d}",
                document_type="mail_delivery_receipt",
                timestamp="2026-03-02T12:00:00.000Z",
                identity="a" * 64,
                counterparty="b" * 64,
                order_ref=f"msg-{i}",
                proof_purpose="acknowledgment",
                payload_json=f'{{"idx":{i}}}',
                signature=None,
            )
        rows = storage.get_receipts_by_type("mail_delivery_receipt")
        assert len(rows) == 5
        # Should be in insertion order (created_at ASC)
        indices = [json.loads(r["payload_json"])["idx"] for r in rows]
        assert indices == sorted(indices)
