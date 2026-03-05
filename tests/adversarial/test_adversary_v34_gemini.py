import pytest
import json
import hashlib
import base64
import time
import asyncio
from typing import List, Dict, Any, Optional, Callable
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

# We'll use our own FakeNode and StorageStub to be sure of the implementation under test.

class StorageStub:
    """Minimal in-memory storage for receipt tests."""
    def __init__(self):
        import sqlite3
        self._conn = sqlite3.connect(":memory:")
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

class FakeNode:
    """Target implementation of _write_receipt and its dependencies."""
    def __init__(self, signing_key=None):
        self.storage = StorageStub()
        self._signing_key = signing_key
        self.node_info = MagicMock(node_id="provider_node_id")

    def _write_receipt(
        self,
        document_type: str,
        payload: dict,
        counterparty: Optional[str] = None,
        order_ref: Optional[str] = None,
        proof_purpose: str = "assertion",
        sign: bool = False,
    ) -> str:
        # VERBATIM from src/knarr/dht/node.py
        import secrets as _secrets
        from datetime import datetime, timezone as _tz
        import logging
        logger = logging.getLogger("test")

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

        # POTENTIAL CRASH HERE: order_ref[:8]
        logger.debug(
            f"RECEIPT_WRITE type={document_type} id={receipt_id} "
            f"order={order_ref[:8] if order_ref else 'none'} signed={sign}"
        )

        try:
            self.storage.write_receipt(
                receipt_id=receipt_id,
                document_type=document_type,
                timestamp=timestamp,
                identity=self.node_info.node_id,
                counterparty=counterparty,
                order_ref=order_ref,
                proof_purpose=proof_purpose,
                payload_json=payload_json,
                signature=signature,
            )
        except Exception as _exc:
            logger.warning(f"RECEIPT_WRITE_FAIL type={document_type} id={receipt_id}: {_exc}")

        return receipt_id

    def _sign_receipt(self, task_id, skill_name, consumer_node_id, credits_charged,
                      input_hash, output_hash, wall_ms,
                      price_breakdown_json=None) -> str:
        # VERBATIM from src/knarr/dht/node.py (OLD FORMAT)
        if not self._signing_key:
            return ""
        import base64
        payload_dict = {
            "task_id": task_id,
            "skill_name": skill_name,
            "provider_node_id": self.node_info.node_id,
            "consumer_node_id": consumer_node_id,
            "credits_charged": credits_charged,
            "input_hash": input_hash,
            "output_hash": output_hash,
            "wall_ms": wall_ms,
            "price_breakdown": json.loads(price_breakdown_json) if price_breakdown_json else None,
            "timestamp": int(time.time()),
        }
        payload_bytes = json.dumps(payload_dict, sort_keys=True, separators=(',', ':')).encode('utf-8')
        signature = self._signing_key.sign(payload_bytes).signature

        receipt_dict = {"data": payload_dict, "signature": base64.b64encode(signature).decode('utf-8')}
        return json.dumps(receipt_dict, sort_keys=True, separators=(',', ':'))

class TestAdversaryV34Gemini:

    # 1. Payload mutation side effect
    def test_payload_mutated_in_place(self):
        node = FakeNode()
        original_payload = {"domain_field": "val"}
        node._write_receipt("execution_receipt", original_payload)
        # Finding: original_payload was modified by _write_receipt
        assert "receipt_id" in original_payload, "Payload mutation: receipt_id injected into caller's dict"
        assert "document_type" in original_payload, "Payload mutation: document_type injected into caller's dict"

    # 2. Crash on non-string order_ref
    def test_write_receipt_crashes_on_int_order_ref(self):
        node = FakeNode()
        # This will fail because of order_ref[:8] in the logger.debug call
        with pytest.raises(TypeError) as excinfo:
            node._write_receipt("execution_receipt", {}, order_ref=123)
        assert "'int' object is not subscriptable" in str(excinfo.value)

    # 3. Signature=None but cryptosuite set
    def test_cryptosuite_set_even_if_signature_none(self):
        node = FakeNode(signing_key=None) 
        node._write_receipt("execution_receipt", {}, sign=True)
        row = node.storage.get_receipts_by_type("execution_receipt")[0]
        payload = json.loads(row["payload_json"])
        # BUG: cryptosuite is present but signature is NULL
        assert payload.get("cryptosuite") == "ed25519-jcs"
        assert row["signature"] is None, "Inconsistency: cryptosuite set but signature is missing"

    # 4. caller_node_id crash on invalid hex (node.py:384)
    def test_caller_node_id_crash_invalid_hex(self):
        # Simulation of node.py:384
        invalid_hex = "not-hex"
        with pytest.raises(ValueError):
             bytes.fromhex(invalid_hex)

    # 5. NaN/Inf in skill_price (node.py:573)
    def test_receipt_with_nan_price(self):
        node = FakeNode()
        price = float('nan')
        node._write_receipt("execution_receipt", {"settlement": {"amount": price}})
        row = node.storage.get_receipts_by_type("execution_receipt")[0]
        # json.dumps(float('nan')) produces 'NaN' which is NOT standard JSON (RFC 8259)
        # Many parsers will fail on this.
        assert "NaN" in row["payload_json"]

    # 6. HTTP Rejection - small packet b'GET' (node.py:2331)
    def test_http_rejection_partial_verb(self):
        peek_bytes = b'GET' # Client closed after 3 bytes
        http_verbs = (b'GET ', b'POST', b'PUT ', b'DELE', b'HEAD', b'OPTI', b'PATC')
        # b'GET' is NOT in http_verbs (which expects b'GET ')
        assert peek_bytes[:4] not in http_verbs

    # 7. HTTP Rejection - lowercase 'get '
    def test_http_rejection_lowercase(self):
        peek_bytes = b'get '
        http_verbs = (b'GET ', b'POST', b'PUT ', b'DELE', b'HEAD', b'OPTI', b'PATC')
        assert peek_bytes[:4] not in http_verbs

    # 8. Sync path schema inconsistency: Timestamp format
    def test_sync_path_timestamp_format_inconsistency(self):
        # Async: custom strftime
        _now = datetime.now(timezone.utc)
        async_ts = _now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_now.microsecond // 1000:03d}Z"
        # Sync: isoformat()
        sync_ts = _now.isoformat()
        # isoformat() -> '2026-03-02T14:00:00.123456+00:00'
        # async_ts   -> '2026-03-02T14:00:00.123Z'
        assert async_ts != sync_ts

    # 9. Sync path schema inconsistency: Signature format
    def test_sync_path_signature_format_inconsistency(self):
        raw_sig = b"\x01" * 64
        async_sig = "ed25519:" + raw_sig.hex()
        sync_sig = base64.b64encode(raw_sig).decode('ascii')
        assert async_sig != sync_sig

    # 10. Mail receipt hasattr guard (sync.py)
    def test_mail_receipt_missing_method_silence(self):
        class NodeWithoutMethod:
            pass
        node = NodeWithoutMethod()
        # hasattr check silently skips. No warning.
        assert not hasattr(node, '_write_receipt')

    # 11. Mail receipt hash json vs str (sync.py:121)
    def test_mail_receipt_hash_json_vs_str(self):
        body = [1, 2, 3]
        body_json = json.dumps(body) # "[1, 2, 3]" (or "[1,2,3]")
        body_str = str(body)         # "[1, 2, 3]"
        # Actually str(list) and json.dumps(list) might match if no spaces, but
        # for more complex objects they won't.
        body_complex = {"a": None}
        assert json.dumps(body_complex) == '{"a": null}'
        assert str(body_complex) == "{'a': None}" # Single quotes vs double, null vs None
        h1 = hashlib.sha256(json.dumps(body_complex).encode()).hexdigest()
        h2 = hashlib.sha256(str(body_complex).encode()).hexdigest()
        assert h1 != h2

    # 12. Mail receipt duplicate missing payload_hash (sync.py:456)
    def test_mail_duplicate_receipt_missing_hash(self):
        # stored case (L424): payload includes payload_hash
        # duplicate case (L456): payload EXCLUDES payload_hash
        # This makes querying by payload_hash inconsistent.
        pass

    # 13. _write_receipt silent data loss
    def test_write_receipt_swallows_exception_silent_loss(self):
        node = FakeNode()
        node.storage.write_receipt = MagicMock(side_effect=RuntimeError("disk full"))
        # Should not raise, returns receipt_id as if successful
        rid = node._write_receipt("execution_receipt", {})
        assert rid is not None
        # But it's NOT in storage
        assert node.storage.get_receipt(rid) is None

    # 14. Sync path: Double Signing (node.py:2142)
    def test_sync_path_double_signing(self):
        # Simulation of sync path
        from nacl.signing import SigningKey
        sk = SigningKey.generate()
        node = FakeNode(signing_key=sk)
        
        # 1. First signature via _sign_receipt
        receipt_json = node._sign_receipt("task-1", "test", "cons", 1.0, "", "", 0)
        parsed_receipt = json.loads(receipt_json)
        assert "signature" in parsed_receipt
        
        # 2. Second signature in node.py:2142
        canonical = json.dumps(parsed_receipt, sort_keys=True, separators=(',', ':')).encode('utf-8')
        sig2 = base64.b64encode(sk.sign(canonical).signature).decode('ascii')
        
        # This sig2 is a signature OF a signed receipt.
        assert sig2 is not None

    # 15. Reader._buffer private API usage
    def test_private_api_usage_fragility(self):
        # StreamReader._buffer is an internal list of bytes in asyncio
        # Assigning to reader._buffer[0:0] works today but is undocumented and fragile.
        pass

    # 16. entropy of receipt_id (secrets.token_hex(6))
    def test_receipt_id_entropy_collision_risk(self):
        # 48 bits entropy is low for a long-running high-volume node.
        # token_hex(6) = 12 chars.
        import secrets
        ids = set()
        for _ in range(1000):
            ids.add(secrets.token_hex(6))
        assert len(ids) == 1000

    # 17. started_at wall_ms=0 (node.py:571)
    def test_started_at_with_zero_wall_ms(self):
        _now = datetime.now(timezone.utc)
        wall_ms = 0
        _started_at = _now - __import__("datetime").timedelta(milliseconds=wall_ms)
        assert _now == _started_at

    # 18. started_at wall_ms negative
    def test_started_at_with_negative_wall_ms(self):
        _now = datetime.now(timezone.utc)
        wall_ms = -1000
        _started_at = _now - __import__("datetime").timedelta(milliseconds=wall_ms)
        assert _started_at > _now # started_at in the future

    # 19. created_at REAL NOT NULL vs storage
    def test_created_at_provided_by_storage(self):
        node = FakeNode()
        rid = node._write_receipt("execution_receipt", {})
        row = node.storage.get_receipt(rid)
        assert row["created_at"] > 0

    # 20. Sync path using OLD format for NEW receipt_log table
    def test_sync_path_old_format_payload(self):
        node = FakeNode()
        # _write_receipt uses flattened payload with "document_type", "version" etc.
        # _sign_receipt uses nested payload with "data" and "signature"
        pass

    # 21. duplicated logic for fail receipts (DRY)
    def test_dry_violation_fail_receipts(self):
        # L738, L796, L858 are copy-pasted.
        pass

    # 22. workers saturated path missing receipt (node.py:2989)
    def test_missing_receipt_on_saturation(self):
        # No receipt written when task is rejected due to queue full
        pass

    # 23. proof_purpose inconsistency
    def test_proof_purpose_mismatch(self):
        # Nothing prevents writing "assertion" when it should be "acknowledgment"
        pass
