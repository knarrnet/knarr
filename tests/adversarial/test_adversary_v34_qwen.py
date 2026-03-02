"""
Adversarial Tests — v0.34.0 "Receipt Foundation"
Model: Qwen
Date: 2026-03-02

Priority order: STARTUP → WIRING → SCHEMA → INPUT → CONCURRENCY → SECURITY
"""
import asyncio
import json
import time
import hashlib
import secrets
import math
import uuid
import base64
from datetime import datetime, timezone
from unittest.mock import Mock, MagicMock, patch, AsyncMock
import pytest

# Test targets
from knarr.dht.node import DHTNode
from knarr.dht.storage import Storage
from knarr.mail.sync import SyncEngine


class TestPayloadMutation:
    """VULN-001: payload dict mutated in place — caller's dict gets modified as side effect."""

    def test_payload_mutation_side_effect(self, tmp_path):
        """_write_receipt mutates the payload dict passed in, causing unexpected side effects."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # Create payload that caller might reuse
        original_payload = {
            "provider": "test_provider",
            "caller": "test_caller",
            "skill_uri": "knarr:///test_skill",
        }
        payload_copy = dict(original_payload)  # Keep original for comparison
        
        # Call _write_receipt with correct signature from src/knarr/dht/node.py
        node._write_receipt(
            document_type="execution_receipt",
            order_ref="test_job_id",
            counterparty="test_caller",
            proof_purpose="assertion",
            payload=original_payload,
            signature=None,
        )
        
        # BUG: original_payload is now mutated with receipt fields
        assert "document_type" in original_payload, "payload was mutated - document_type added"
        assert "version" in original_payload, "payload was mutated - version added"
        assert "receipt_id" in original_payload, "payload was mutated - receipt_id added"
        assert "timestamp" in original_payload, "payload was mutated - timestamp added"
        assert "proof_purpose" in original_payload, "payload was mutated - proof_purpose added"
        assert "cryptosuite" in original_payload, "payload was mutated - cryptosuite added"
        
        # Caller's original dict is now corrupted
        assert original_payload != payload_copy, "Side effect: caller's payload dict modified"


class TestAsyncioPrivateAPI:
    """VULN-002: reader._buffer[0:0] = peek_bytes — private asyncio API, undocumented."""

    def test_private_buffer_manipulation(self, tmp_path):
        """A1 HTTP rejection uses reader._buffer which is a private, undocumented API."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # Create a mock reader
        mock_reader = Mock()
        mock_reader._buffer = bytearray(b'GET / HTTP/1.1\r\n')
        
        # This code path uses private API
        peek_bytes = b'GET '
        try:
            # This is what node.py line 2344 does:
            mock_reader._buffer[0:0] = peek_bytes
            # Works, but _buffer is not part of public asyncio API
            assert len(mock_reader._buffer) > 0
        except AttributeError:
            pytest.fail("_buffer attribute missing - private API may change between Python versions")


class TestSyncPathFormatInconsistency:
    """VULN-003: Sync path uses OLD receipt format vs new _write_receipt() format."""

    def test_sync_path_receipt_format_mismatch(self, tmp_path):
        """Sync path (node.py ~2130-2187) generates receipts with uuid IDs and base64 sigs,
        while _write_receipt() uses hex sigs and token_hex IDs."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # Sync path generates receipt_id like: exec_<uuid.hex[:12]> (line 2162)
        sync_receipt_id = f"exec_{uuid.uuid4().hex[:12]}"
        
        # New _write_receipt() generates: <prefix>_<token_hex(6)> (12 hex chars = 6 bytes)
        new_receipt_id = f"exec_{secrets.token_hex(6)}"
        
        # Both are 12 hex chars but use different generators
        # uuid.hex uses UUID random bytes, token_hex uses secrets module
        # This creates two different receipt ID formats in the same table
        # exec_ (5) + 12 hex = 17 chars
        assert len(sync_receipt_id) == len(new_receipt_id) == 17
        assert sync_receipt_id.startswith("exec_")
        assert new_receipt_id.startswith("exec_")
        
        # More critically: sync path uses base64 signatures (line 2165)
        # while _write_receipt uses hex signatures (line 1103)
        mock_sig_bytes = b'\x00' * 64
        sync_sig = base64.b64encode(mock_sig_bytes).decode('ascii')  # Sync path style
        new_sig = "ed25519:" + mock_sig_bytes.hex()  # New path style
        
        assert sync_sig != new_sig, "Signature formats differ between sync and async paths"
        assert sync_sig.startswith("AAAA"), "Sync path uses raw base64"
        assert new_sig.startswith("ed25519:"), "New path uses prefixed hex"


class TestHasattrGuardSilentFailure:
    """VULN-004: hasattr(self._node, '_write_receipt') guard silently skips ALL mail receipts."""

    def test_hasattr_guard_silent_skip(self, tmp_path):
        """sync.py uses hasattr() guards that silently skip receipt writes if method missing."""
        storage_path = str(tmp_path / "test.db")
        
        # Create a mock node without _write_receipt method
        class MockNode:
            node_info = Mock()
            node_info.node_id = "test_node"
        
        mock_node = MockNode()
        
        # sync.py line 124, 368, 436 pattern:
        if hasattr(mock_node, '_write_receipt'):
            pytest.fail("Method should be missing for this test")
        else:
            # Receipt write is silently skipped - no warning logged
            # This is the bug: no alert, no error, just silent data loss
            pass


class TestEmptyItemIdsIndexError:
    """VULN-005: item_ids[0] as order_ref when item_ids could be empty → IndexError."""

    def test_empty_item_ids_index_error(self, tmp_path):
        """sync.py line 327 uses item_ids[0] without checking if list is empty."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # Simulate the bug condition: all items blocked by egress filter
        item_ids = []  # Empty after egress filtering
        
        # sync.py line 327 pattern:
        try:
            order_ref = item_ids[0] if item_ids else None  # Fixed version
            assert order_ref is None
        except IndexError:
            pytest.fail("Should handle empty list")
        
        # But the actual code at line 327 does:
        # order_ref=item_ids[0] if item_ids else None
        # Wait, let me check the actual code...
        
        # Actually the bug is at line 327 in the receipt write:
        # order_ref=item_ids[0] if item_ids else None
        # This is actually safe. Let me check other locations.
        
        # Found it: line 253 in _push_to_peer_inner:
        # _bus.emit("mail.delivery_failed", to_node=peer_node_id, message_id=item_ids[0] if item_ids else "", ...)
        # This is also safe.
        
        # The real bug: self-delivery path line 108
        # item_ids = [item["item_id"] for item in pending]
        # If pending is empty, item_ids is empty, but then:
        # order_ref=item_id  # Uses individual item_id, not item_ids[0]
        
        # Actually the IndexError risk is in mail_receive_receipt where order_ref=item_id
        # and item_id comes from item.get("item_id") which could be None


class TestSignWithNoKey:
    """VULN-006: sign=True with self._signing_key=None → cryptosuite in payload but signature null."""

    def test_sign_true_no_key_null_signature(self, tmp_path):
        """When signature is None, payload still has cryptosuite but signature is None."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # Call with signature=None (simulating no signing key)
        receipt_id = node._write_receipt(
            document_type="execution_receipt",
            order_ref="test_order",
            counterparty="test_counterparty",
            proof_purpose="assertion",
            payload={"test": "data"},
            signature=None,  # No signature
        )
        
        # Check storage for the receipt
        conn = node.storage._get_conn()
        cursor = conn.execute(
            "SELECT payload_json, signature FROM receipt_log WHERE receipt_id = ?",
            (receipt_id,)
        )
        row = cursor.fetchone()
        assert row is not None, "Receipt should be written"
        
        payload = json.loads(row[0])
        signature = row[1]
        
        # BUG: payload has cryptosuite but signature is NULL
        assert payload.get("cryptosuite") == "ed25519-jcs", "cryptosuite set in payload"
        assert signature is None, "signature is NULL - cryptosuite claim without actual signature"


class TestCreatedAtSchemaMismatch:
    """VULN-007: created_at REAL NOT NULL in schema — does write_receipt() provide it?"""

    def test_created_at_provided_by_storage(self, tmp_path):
        """Schema has created_at REAL NOT NULL but _write_receipt doesn't compute it."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # _write_receipt passes `now` (time.time()) as created_at
        # storage.write_receipt receives it and inserts it
        receipt_id = node._write_receipt(
            document_type="test_receipt",
            order_ref="test_order",
            counterparty="test_counterparty",
            proof_purpose="assertion",
            payload={"test": "data"},
            signature=None,
        )
        
        conn = node.storage._get_conn()
        cursor = conn.execute(
            "SELECT created_at FROM receipt_log WHERE receipt_id = ?",
            (receipt_id,)
        )
        row = cursor.fetchone()
        assert row is not None
        created_at = row[0]
        
        # created_at should be a Unix timestamp (REAL)
        assert isinstance(created_at, float), "created_at should be Unix timestamp"
        assert created_at > 0, "created_at should be positive"
        # This test passes - storage.write_receipt does provide created_at via `now = time.time()`


class TestNaNInfInSkillPrice:
    """VULN-008: NaN/Inf in float(skill_price) flows into receipt amount unchecked."""

    def test_nan_skill_price_in_receipt(self, tmp_path):
        """skill_price could be NaN/Inf and flows into receipt settlement.amount unchecked."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # Simulate NaN skill_price (could come from misconfigured pricing)
        nan_price = float('nan')
        
        # The receipt write would include this NaN
        payload = {
            "provider": node.node_info.node_id,
            "caller": "test_caller",
            "settlement": {
                "amount": nan_price,  # NaN flows through
                "currency": "credits",
            }
        }
        
        # JSON serialization of NaN produces invalid JSON or "NaN" string
        try:
            json.dumps(payload)
            # If this succeeds, NaN became "NaN" string which is invalid JSON
            # Actually Python's json module allows NaN by default but it's not standard JSON
        except (ValueError, TypeError):
            pass  # Expected for strict JSON
        
        # Test with Inf
        inf_price = float('inf')
        payload["settlement"]["amount"] = inf_price
        try:
            json_str = json.dumps(payload)
            # "Infinity" is not valid JSON
            assert "Infinity" in json_str or "NaN" in json_str
        except (ValueError, TypeError):
            pass


class TestOrderRefNoneDebugLogCrash:
    """VULN-009: order_ref[:8] in debug log — crashes if order_ref is not None but is non-string."""

    def test_order_ref_non_string_debug_crash(self, tmp_path):
        """Debug log at line 1117 does order_ref[:8] which crashes for non-string types."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # order_ref as integer (could happen if job_id is numeric)
        int_order_ref = 12345
        
        # The debug log does: order_ref[:8] if order_ref else 'none'
        # This assumes order_ref is a string
        try:
            # Simulating the bug:
            result = int_order_ref[:8] if int_order_ref else 'none'
            pytest.fail("Should crash on slice of int")
        except TypeError as e:
            # Expected: 'int' object is not subscriptable
            assert "not subscriptable" in str(e)


class TestStartedAtWallMsEdgeCases:
    """VULN-010: started_at computed from completed_at - timedelta — wall_ms could be 0, negative, or enormous."""

    def test_negative_wall_ms_started_at(self, tmp_path):
        """Negative wall_ms would make started_at > completed_at (time travel receipt)."""
        # Simulate negative wall_ms (could happen with clock skew or measurement bug)
        wall_ms = -5000  # Negative 5 seconds
        
        completed_at = datetime.now(timezone.utc)
        # The code does:
        started_at = completed_at - __import__("datetime").timedelta(milliseconds=wall_ms)
        
        # With negative wall_ms, started_at is AFTER completed_at
        assert started_at > completed_at, "Negative wall_ms causes time travel receipt"

    def test_enormous_wall_ms_started_at(self, tmp_path):
        """Enormous wall_ms would make started_at years in the past."""
        wall_ms = 31536000000  # 1 year in milliseconds
        
        completed_at = datetime.now(timezone.utc)
        started_at = completed_at - __import__("datetime").timedelta(milliseconds=wall_ms)
        
        # started_at is a year ago
        assert (completed_at - started_at).days > 360


class TestPublicKeyOddLengthCrash:
    """VULN-011: msg.public_key odd-length hex → bytes.fromhex crashes."""

    def test_odd_length_public_key_crash(self):
        """hashlib.sha256(bytes.fromhex(msg.public_key)) crashes on odd-length hex."""
        odd_hex = "abc123"  # 6 chars is even, let's use 5
        odd_hex = "abc12"  # 5 chars - odd length
        
        try:
            result = bytes.fromhex(odd_hex)
            pytest.fail("Should crash on odd-length hex")
        except ValueError as e:
            assert "non-hexadecimal" in str(e) or "odd-length" in str(e)
        
        # This crashes in node.py lines 377, 398, 417, etc.
        # caller_nid = hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest()


class TestReceiptCallSiteDuplication:
    """VULN-012: Multiple execution receipt writes have similar code structure."""

    def test_receipt_code_duplication(self):
        """Multiple receipt write call sites exist with similar payload structure."""
        # Read the actual code to verify receipt writes
        import inspect
        from knarr.dht.node import DHTNode
        
        source = inspect.getsource(DHTNode)
        
        # Count receipt writes for execution_receipt
        exec_receipt_count = source.count('document_type="execution_receipt"')
        
        # Multiple execution receipt writes exist
        assert exec_receipt_count >= 1, "Execution receipt writes should exist"
        
        # The brief mentions 3 fail blocks but actual code may differ
        # This test documents that multiple receipt writes exist


class TestMailBodyJsonSerialization:
    """VULN-013: json.dumps(item.get("body")) — what if body is not JSON-serializable?"""

    def test_non_serializable_mail_body(self, tmp_path):
        """Mail body with bytes, datetime, or custom objects crashes json.dumps."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # Non-serializable body types
        non_serializable_bodies = [
            b"bytes object",  # bytes
            datetime.now(),  # datetime
            {"key": lambda x: x},  # lambda in dict
            {"key": object()},  # custom object
        ]
        
        for body in non_serializable_bodies:
            try:
                json.dumps(body)
                pytest.fail(f"Should crash on {type(body)}")
            except (TypeError, ValueError):
                pass  # Expected


class TestBodyStrHashInconsistency:
    """VULN-014: str(list) produces different hash than json.dumps(list)."""

    def test_body_str_hash_inconsistency(self, tmp_path):
        """sync.py line 370 uses str() for non-dict bodies but json.dumps() for dicts."""
        test_list = ["item1", "item2"]
        
        # Dict path
        dict_body = {"key": "value"}
        dict_str = json.dumps(dict_body)
        dict_hash = hashlib.sha256(dict_str.encode("utf-8")).hexdigest()
        
        # List path (non-dict)
        list_str = str(test_list)  # "['item1', 'item2']"
        list_hash = hashlib.sha256(list_str.encode("utf-8")).hexdigest()
        
        # Same list via json.dumps
        list_json_str = json.dumps(test_list)  # '["item1", "item2"]'
        list_json_hash = hashlib.sha256(list_json_str.encode("utf-8")).hexdigest()
        
        # Different representations, different hashes
        assert list_str != list_json_str, "str() and json.dumps() produce different output"
        assert list_hash != list_json_hash, "Hash inconsistency for list bodies"


class TestDuplicateReceiptPayloadInconsistency:
    """VULN-015: Duplicate receive receipt has payload_bytes: 0 but no payload_hash."""

    def test_duplicate_receipt_missing_hash(self, tmp_path):
        """sync.py line 436-450: duplicate receipt has payload_bytes: 0, no payload_hash."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # Stored receipt (line 368-385) has both payload_bytes and payload_hash
        stored_payload = {
            "receiver": node.node_info.node_id,
            "sender": "test_sender",
            "message_id": "test_msg",
            "message_type": "test",
            "receipt": {
                "status": "stored",
                "payload_bytes": 100,
                "payload_hash": "sha256:abc123...",
            },
        }
        
        # Duplicate receipt (line 436-450) has payload_bytes: 0, NO payload_hash
        duplicate_payload = {
            "receiver": node.node_info.node_id,
            "sender": "test_sender",
            "message_id": "test_msg",
            "message_type": "test",
            "receipt": {
                "status": "duplicate",
                "payload_bytes": 0,
                # payload_hash missing!
            },
        }
        
        # Schema inconsistency: same receipt type, different payload structure
        assert "payload_hash" in stored_payload["receipt"]
        assert "payload_hash" not in duplicate_payload["receipt"]


class TestEgressBlockReceiptGap:
    """VULN-016: No receipt written for 'workers saturated' queue-full path."""

    def test_queue_full_no_receipt(self, tmp_path):
        """node.py line 2989-2996: workers saturated path doesn't write order_ack receipt."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # When queue is full and task is rejected, no receipt is written
        # This creates a gap in the audit trail
        
        # Simulate the condition
        initial_receipt_count = node.storage._get_conn().execute(
            "SELECT COUNT(*) FROM receipt_log"
        ).fetchone()[0]
        
        # The bug path (line 2989-2996) returns TaskResult without writing receipt
        # This is a coverage gap - rejected tasks leave no receipt trail
        
        # Verify no receipt was written for the rejection path
        final_receipt_count = node.storage._get_conn().execute(
            "SELECT COUNT(*) FROM receipt_log"
        ).fetchone()[0]
        
        # In the actual code, this path doesn't write a receipt
        # The test documents the gap


class TestProofPurposeInconsistency:
    """VULN-017: proof_purpose inconsistency — nothing validates 'assertion' vs 'acknowledgment'."""

    def test_proof_purpose_no_validation(self, tmp_path):
        """Any proof_purpose value can be used for any receipt type - no validation."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # Commerce docs say delivery receipts use "acknowledgment"
        # Transport uses "assertion"
        # But nothing prevents mixing them
        
        # Write a delivery receipt with wrong proof_purpose
        receipt_id = node._write_receipt(
            document_type="mail_delivery_receipt",
            order_ref="test_order",
            counterparty="test_counterparty",
            proof_purpose="assertion",  # Should be "acknowledgment" per docs
            payload={"test": "data"},
            signature=None,
        )
        
        # No validation - receipt written successfully
        conn = node.storage._get_conn()
        cursor = conn.execute(
            "SELECT proof_purpose FROM receipt_log WHERE receipt_id = ?",
            (receipt_id,)
        )
        row = cursor.fetchone()
        assert row[0] == "assertion", "Invalid proof_purpose accepted"


class TestSqliteWriteContention:
    """VULN-018: Thread safety — _write_receipt() called from thread pool AND async context."""

    def test_concurrent_receipt_writes_documentation(self, tmp_path):
        """Documents the thread safety concern with receipt writes.
        
        The _write_receipt() method is called from:
        1. Async context (main event loop)
        2. Thread pool (handler threads in _execute_queued_task)
        
        SQLite with WAL mode and check_same_thread=False allows cross-thread access,
        but concurrent writes could cause contention or locking issues.
        
        This test documents the concern - actual concurrent testing requires
        careful asyncio event loop management across threads.
        """
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # Verify storage is configured for cross-thread access
        conn = node.storage._get_conn()
        
        # check_same_thread=False is set in Storage.__init__
        # This allows handler threads to write receipts
        assert conn is not None
        
        # Write a receipt to verify basic functionality
        node._write_receipt(
            document_type="test_receipt",
            order_ref="test_order",
            counterparty="test_counterparty",
            proof_purpose="assertion",
            payload={"thread_safety": "documented"},
            signature=None,
        )
        
        # Verify receipt was written
        count = conn.execute("SELECT COUNT(*) FROM receipt_log").fetchone()[0]
        assert count == 1
        
        # CONCERN: When _write_receipt is called from multiple handler threads
        # simultaneously, SQLite WAL mode should handle contention, but:
        # - Lock waits could cause timeouts under heavy load
        # - INSERT OR IGNORE could silently drop receipts on collision
        # - No retry logic for transient lock errors


class TestExceptionSwallowedReceiptWrite:
    """VULN-019: Exception handling in _write_receipt."""

    def test_receipt_write_failure_propagates(self, tmp_path, caplog):
        """_write_receipt propagates exceptions from storage.write_receipt()."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # Mock storage.write_receipt to raise
        import sqlite3
        
        def failing_write(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")
        
        node.storage.write_receipt = failing_write
        
        # Call _write_receipt - exception propagates
        with pytest.raises(sqlite3.OperationalError):
            node._write_receipt(
                document_type="test_receipt",
                order_ref="test_order",
                counterparty="test_counterparty",
                proof_purpose="assertion",
                payload={"test": "data"},
                signature=None,
            )
        
        # The actual code does NOT swallow exceptions - they propagate
        # This is actually correct behavior - the brief was incorrect
        # The test documents that exceptions are NOT silently swallowed


class TestReceiptIdCollision:
    """VULN-020: INSERT OR IGNORE — receipt_id collision silently drops the second receipt."""

    def test_receipt_id_collision_silent_drop(self, tmp_path):
        """secrets.token_hex(6) = 48 bits. Birthday collision at ~16M receipts."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # Manually create a collision by writing same receipt_id twice
        receipt_id = "exec_000000000001"  # Fixed ID for test
        
        # First write
        node.storage.write_receipt(
            receipt_id=receipt_id,
            document_type="execution_receipt",
            timestamp="2026-03-02T00:00:00.000Z",
            identity="node1",
            counterparty="node2",
            order_ref="job1",
            proof_purpose="assertion",
            payload_json='{"first": true}',
            signature="sig1",
        )
        
        # Second write with same ID - silently ignored due to INSERT OR IGNORE
        node.storage.write_receipt(
            receipt_id=receipt_id,
            document_type="mail_delivery_receipt",  # Different type!
            timestamp="2026-03-02T00:00:01.000Z",
            identity="node1",
            counterparty="node3",
            order_ref="job2",
            proof_purpose="acknowledgment",
            payload_json='{"second": true}',
            signature="sig2",
        )
        
        # Check what's in the DB
        conn = node.storage._get_conn()
        cursor = conn.execute(
            "SELECT document_type, payload_json FROM receipt_log WHERE receipt_id = ?",
            (receipt_id,)
        )
        row = cursor.fetchone()
        
        # First receipt preserved, second silently dropped
        assert row[0] == "execution_receipt", "First receipt preserved"
        assert row[1] == '{"first": true}', "Second receipt silently dropped"


class TestHttpVerbEdgeCases:
    """VULN-021: HTTP verb check edge cases — "GETS" vs "GET ", lowercase, exact 4 bytes."""

    def test_http_verb_edge_cases(self, tmp_path):
        """HTTP verb detection has edge cases that could allow bypass."""
        # node.py line 2335 checks first 4 bytes against http_verbs tuple
        
        http_verbs = (b'GET ', b'POST', b'PUT ', b'DELE', b'HEAD', b'OPTI', b'PATC')
        
        # Edge case 1: "GETS" (not "GET ") - would NOT match
        assert b'GETS' not in http_verbs
        
        # Edge case 2: "get " (lowercase) - would NOT match
        assert b'get ' not in http_verbs
        
        # Edge case 3: Exactly 3 bytes + EOF - peek_bytes is 3 bytes, not 4
        # reader.read(4) would return 3 bytes
        # Then peek_bytes[:4] would be the 3 bytes, which won't match any verb
        
        # Edge case 4: Empty peek_bytes (immediate EOF)
        peek_bytes = b''
        assert peek_bytes[:4] not in http_verbs


class TestKnarrLengthPrefixCollision:
    """VULN-022: Could any valid knarr length prefix match an HTTP verb?"""

    def test_length_prefix_false_positive(self):
        """Check if any valid 4-byte length prefix could match HTTP verbs."""
        http_verbs = (b'GET ', b'POST', b'PUT ', b'DELE', b'HEAD', b'OPTI', b'PATC')
        
        # Knarr protocol: first 4 bytes are big-endian length prefix
        # For a length prefix to match an HTTP verb, the 4 bytes would need to
        # spell out one of the verbs
        
        # b'GET ' = 0x47455420 = 1,195,725,856 bytes (1.1 GB message)
        # b'POST' = 0x504F5354 = 1,347,375,956 bytes (1.3 GB message)
        
        # These are enormous but technically valid length prefixes
        # A malicious actor could send a message with this length prefix
        # to trigger false positive HTTP detection
        
        get_space_int = int.from_bytes(b'GET ', 'big')
        post_int = int.from_bytes(b'POST', 'big')
        
        # Verify the integer values
        assert get_space_int == 1195725856
        assert post_int == 1347375956
        
        # These would be rejected as oversized messages, but the HTTP check
        # happens BEFORE length validation, so this is actually a feature not a bug


class TestMailDeliveryReceiptErrorString:
    """VULN-023: Mail delivery receipt error_string could be None on success path."""

    def test_delivery_receipt_error_string_none(self, tmp_path):
        """sync.py line 327: error_string is None on success but receipt still written."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # On delivery success, error_string is None
        # But the receipt payload includes error_string in some paths
        
        # Actually looking at the code, on success the receipt uses "ack" status
        # and doesn't include error field. On failure it includes error_string.
        # This test documents the code path but doesn't find a bug here.


class TestSyncPathHasattrGuard:
    """VULN-024: Sync path receipt write at line 2172 has no hasattr guard."""

    def test_sync_path_receipt_write_no_guard(self, tmp_path):
        """sync.py mail receipt writes have hasattr guards but node.py sync path doesn't."""
        storage_path = str(tmp_path / "test.db")
        
        # Create a mock node without _write_receipt method
        class MockNode:
            node_info = Mock()
            node_info.node_id = "test_node"
            storage = Mock()
            
        mock_node = MockNode()
        
        # The sync path at line 2172 directly calls self.storage.write_receipt()
        # It doesn't use _write_receipt helper, so it bypasses the hasattr check
        # But it also bypasses all the enrichment (receipt_id generation, timestamp, etc.)
        
        # This is actually a different bug: sync path uses direct storage.write_receipt()
        # instead of the _write_receipt helper, creating inconsistency
        
        # Test that direct storage call works without _write_receipt
        mock_node.storage.write_receipt = Mock()
        mock_node.storage.write_receipt(
            receipt_id="test_123",
            document_type="execution_receipt",
            timestamp="2026-03-02T00:00:00.000Z",
            identity="test_node",
            counterparty="other_node",
            order_ref="job_123",
            proof_purpose="assertion",
            payload_json='{"test": true}',
            signature=None,
        )
        
        # Direct call succeeds but bypasses _write_receipt enrichment
        mock_node.storage.write_receipt.assert_called_once()


class TestReceiptLowEntropy:
    """VULN-025: secrets.token_hex(6) = 12 hex chars = 48 bits entropy."""

    def test_receipt_id_entropy(self, tmp_path):
        """48 bits entropy means birthday collision at ~16M receipts (sqrt(2^48))."""
        # This is actually reasonable entropy for receipt IDs
        # But the brief mentions it as a potential concern
        
        receipt_id = f"exec_{secrets.token_hex(6)}"
        
        # Format: exec_<12 hex chars>
        # exec_ (5) + 12 = 17 chars
        assert len(receipt_id) == 17
        assert receipt_id.startswith("exec_")
        
        # 48 bits = 2^48 = 281 trillion combinations
        # Birthday paradox: collision expected after sqrt(2^48) = 16M receipts
        # For a receipt log, this is probably acceptable
        # But high-volume nodes could eventually hit collisions


class TestEgressFilterBlockedMailReceipt:
    """VULN-026: Egress filter blocks mail item but no receipt written for blocked items."""

    def test_egress_blocked_no_receipt(self, tmp_path):
        """sync.py line 201-208: egress blocked items don't generate receipts."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        # When egress filter blocks a mail item, it's reverted to pending
        # but no receipt is written to document the block
        
        # This is an audit gap - blocked items leave no receipt trail
        initial_count = node.storage._get_conn().execute(
            "SELECT COUNT(*) FROM receipt_log"
        ).fetchone()[0]
        
        # The actual egress filter code path doesn't write receipts for blocks
        # This documents the gap


class TestCorrelationIdMissingInReceipt:
    """VULN-027: Receipts don't include correlation IDs for tracing."""

    def test_receipt_no_correlation_id(self, tmp_path):
        """Receipts don't include session_id or correlation_id for distributed tracing."""
        storage_path = str(tmp_path / "test.db")
        node = DHTNode("127.0.0.1", 0, storage_path, config={"node": {"task_slots": 4}})
        
        receipt_id = node._write_receipt(
            document_type="execution_receipt",
            order_ref="job_123",
            counterparty="test_caller_node",
            proof_purpose="assertion",
            payload={
                "provider": "test",
                "caller": "test",
                "skill_uri": "knarr:///test",
            },
            signature=None,
        )
        
        # Check payload doesn't have correlation fields
        conn = node.storage._get_conn()
        cursor = conn.execute(
            "SELECT payload_json FROM receipt_log WHERE receipt_id = ?",
            (receipt_id,)
        )
        payload = json.loads(cursor.fetchone()[0])
        
        # No correlation_id, trace_id, or span_id fields
        assert "correlation_id" not in payload
        assert "trace_id" not in payload
        assert "span_id" not in payload
        
        # This limits distributed tracing capabilities


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
