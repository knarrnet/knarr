"""Tests for WarehouseManager — v0.37.0 Vordur.

Covers all five gates, three lifecycle paths (auto_promote, hold_for_review,
reject), restart recovery, config rule loading, and edge cases.

All crypto and I/O is mocked. No real Ed25519 signatures in unit tests.
"""

import json
import os
import sqlite3
import sys
import time
import unittest
from unittest.mock import MagicMock, patch
from dataclasses import asdict

# Ensure proposed-a/src is on path before installed knarr package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

# Inline minimal storage stub with quarantine support for isolated testing.


class _QuarantineStorage:
    """In-memory SQLite with dmz_quarantine table for WM tests."""

    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS dmz_quarantine (
                id TEXT PRIMARY KEY,
                document_type TEXT NOT NULL,
                document_json TEXT NOT NULL,
                originator_pubkey TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                gate_results TEXT,
                reason TEXT,
                received_at REAL NOT NULL,
                promoted_at REAL,
                resolved_at REAL
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_dmz_status ON dmz_quarantine(status)")

    def quarantine_store(self, id, document_type, document_json, originator_pubkey, status, gate_results, reason):
        now = time.time()
        if not isinstance(document_json, str):
            document_json = json.dumps(document_json, sort_keys=True)
        if not isinstance(gate_results, str):
            gate_results = json.dumps(gate_results, sort_keys=True)
        self._conn.execute(
            """INSERT OR REPLACE INTO dmz_quarantine
               (id, document_type, document_json, originator_pubkey, status, gate_results, reason, received_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, document_type, document_json, originator_pubkey, status, gate_results, reason, now),
        )
        self._conn.commit()

    def quarantine_get(self, id):
        cursor = self._conn.execute(
            """SELECT id, document_type, document_json, originator_pubkey,
                      status, gate_results, reason, received_at, promoted_at, resolved_at
               FROM dmz_quarantine WHERE id = ?""", (id,))
        row = cursor.fetchone()
        if not row:
            return None
        return dict(zip(
            ["id", "document_type", "document_json", "originator_pubkey",
             "status", "gate_results", "reason", "received_at", "promoted_at", "resolved_at"], row))

    def quarantine_update_status(self, id, status, reason=None, promoted_at=None, resolved_at=None):
        self._conn.execute(
            """UPDATE dmz_quarantine
               SET status = ?, reason = COALESCE(?, reason),
                   promoted_at = COALESCE(?, promoted_at),
                   resolved_at = COALESCE(?, resolved_at)
               WHERE id = ?""",
            (status, reason, promoted_at, resolved_at, id))
        self._conn.commit()

    def quarantine_list_pending(self):
        return self._list_by_status("pending")

    def quarantine_list_by_status(self, status):
        return self._list_by_status(status)

    def _list_by_status(self, status):
        cursor = self._conn.execute(
            """SELECT id, document_type, document_json, originator_pubkey,
                      status, gate_results, reason, received_at, promoted_at, resolved_at
               FROM dmz_quarantine WHERE status = ? ORDER BY received_at ASC""", (status,))
        cols = ["id", "document_type", "document_json", "originator_pubkey",
                "status", "gate_results", "reason", "received_at", "promoted_at", "resolved_at"]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ---------- Helpers ----------

NODE_ID = "a" * 64
IDENTITY_FRAGMENTS = [
    NODE_ID,
    f"did:knarr:{NODE_ID}",
    f"did:knarr:{NODE_ID}#key-1",
    f"did:knarr:{NODE_ID}#cockpit-1",
    f"did:knarr:{NODE_ID}#thrall-1",
]
FAKE_PUBKEY = b"\x01" * 32  # 32-byte Ed25519 public key


def _make_signed_doc(doc_type="credit_note", identity=None, counterparty=None, vm=None):
    """Build a minimal signed document for testing (proof fields included)."""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    doc = {
        "document_type": doc_type,
        "type": f"knarr/commerce/{doc_type}",
        "identity": identity or NODE_ID,
        "counterparty": counterparty or "b" * 64,
        "proof": {
            "type": "DataIntegrityProof",
            "cryptosuite": "eddsa-jcs-2022",
            "verificationMethod": vm or f"did:knarr:{'b' * 64}#key-1",
            "proofPurpose": "assertionMethod",
            "created": now_iso,
            "proofValue": "z" + "A" * 86,  # fake base58 value
        },
    }
    # v0.37.0: Add required body fields so Gate 3 validators pass
    _BODY_FIELDS = {
        "payment_received": {
            "chain_id": "solana-mainnet", "tx_hash": "abc123", "tx_index": 0,
            "from_address": "S", "to_address": "R",
            "amount": 1000, "denom": "KNARR", "decimals": 9,
            "confirmation": {"level": "finalized"},
        },
        "payment_finalized": {
            "chain_id": "solana-mainnet", "tx_hash": "abc123",
            "amount": 1000, "denom": "KNARR",
            "original_receipt_id": "prx_123", "finality": {"level": "finalized"},
        },
        "payment_executed": {
            "chain_id": "solana-mainnet", "tx_hash": "abc",
            "from_address": "S", "to_address": "R",
            "amount": 500, "denom": "KNARR", "decimals": 9,
            "settlement_ref": {"settlement_accepted_id": "sa_123"},
            "finality": {"level": "finalized"},
        },
        "wallet_transfer": {
            "chain_id": "solana-mainnet", "tx_hash": "abc",
            "from_address": "S", "to_address": "R",
            "amount": 100, "denom": "SOL", "decimals": 9,
            "transfer_type": "master_to_derived",
        },
        "wallet_withdrawal": {
            "chain_id": "solana-mainnet", "tx_hash": "abc",
            "from_address": "S", "to_address": "R",
            "amount": 100, "denom": "SOL", "decimals": 9,
        },
        "configuration_order": {
            "target": "exposure_schema", "operation": "upsert_object",
            "changes": {"object_key": "economy.summary"},
        },
        "punchhole_card": {
            "for_node": "abc", "for_access_level": "peer",
            "available": [], "not_available": [],
        },
        "cache_object": {
            "object_key": "economy.summary",
            "data": {"balance": 100}, "granularity": {"balance": "exact"},
        },
        "settlement_prepared": {
            "proposer": NODE_ID, "amount": 5.0, "formula": "bilateral",
            "proposer_balance": -8.0, "counterparty_balance_claimed": 8.0,
            "utilization": 0.8, "target_utilization": 0.5,
        },
        "settlement_accepted": {
            "proposer": NODE_ID, "amount": 5.0,
            "authority": NODE_ID, "authority_method": "auto",
            "prepared_receipt_id": "sp_test",
        },
    }
    if doc_type in _BODY_FIELDS:
        doc.update(_BODY_FIELDS[doc_type])
    return doc


def _make_wm(config_override=None, storage=None):
    """Build a WarehouseManager with mocked dependencies."""
    from knarr.core.warehouse_manager import WarehouseManager

    bus = MagicMock()
    st = storage or _QuarantineStorage()
    write_receipt_cb = MagicMock()
    config = config_override or {"debug": True}
    wm = WarehouseManager(
        node_id=NODE_ID,
        identity_fragments=IDENTITY_FRAGMENTS,
        internal_signer_keys={},
        bus=bus,
        storage=st,
        config=config,
        write_receipt_cb=write_receipt_cb,
    )
    return wm, bus, st, write_receipt_cb


# ---------- Gate 1: Authenticity ----------

class TestGate1Authenticity(unittest.TestCase):
    """Gate 1: signature verification via verify_document."""

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_gate1_pass_valid_signature(self, mock_verify):
        """Valid signature passes gate 1."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("credit_note")
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.gate_results.get(1), "pass")
        mock_verify.assert_called_once()

    @patch("knarr.core.warehouse_manager.verify_document", return_value=False)
    def test_gate1_fail_invalid_signature(self, mock_verify):
        """Invalid signature fails gate 1 and quarantines."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("credit_note")
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.gate_results.get(1), "fail")
        self.assertIn("Gate 1 failed", result.reason)
        # Verify quarantined in storage
        row = st.quarantine_get(result.quarantine_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "rejected")

    @patch("knarr.core.warehouse_manager.verify_document", side_effect=Exception("bad key"))
    def test_gate1_fail_exception(self, mock_verify):
        """Exception in verify_document fails gate 1."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("credit_note")
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.gate_results.get(1), "fail")


# ---------- Gate 2: Addressing ----------

class TestGate2Addressing(unittest.TestCase):
    """Gate 2: document references our identity."""

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_gate2_pass_our_identity(self, mock_verify):
        """Document referencing our node_id passes gate 2."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("credit_note", identity=NODE_ID)
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.gate_results.get(2), "pass")

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_gate2_fail_wrong_identity(self, mock_verify):
        """Document NOT referencing our identity fails gate 2."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("credit_note", identity="c" * 64, counterparty="d" * 64)
        # Also set proof VM to a foreign node
        doc["proof"]["verificationMethod"] = f"did:knarr:{'c' * 64}#key-1"
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.gate_results.get(2), "fail")
        self.assertIn("Gate 2 failed", result.reason)

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_gate2_pass_did_fragment(self, mock_verify):
        """Document referencing a DID fragment passes gate 2."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("credit_note", identity=f"did:knarr:{NODE_ID}#key-1")
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.gate_results.get(2), "pass")

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_gate2_skip_for_payment_types(self, mock_verify):
        """BCW-originated types (payment_received) skip gate 2."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("payment_received", identity="x" * 64)
        result = wm.ingest(doc, FAKE_PUBKEY)
        # payment_received uses gates [1,3,4] — gate 2 should be "skip"
        self.assertEqual(result.gate_results.get(2), "skip")


# ---------- Gate 3: Schema ----------

class TestGate3Schema(unittest.TestCase):
    """Gate 3: known type + valid structure."""

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_gate3_pass_known_type_valid(self, mock_verify):
        """Known type with valid schema passes gate 3."""
        wm, bus, st, wr = _make_wm()
        # credit_note requires specific body fields for validate_credit_note.
        # Since we validate the document itself via body = doc.get("body", doc),
        # we include the fields the validator expects.
        doc = _make_signed_doc("credit_note")
        doc["type"] = "knarr/commerce/credit_note"
        doc["amount"] = 1.5
        doc["reason"] = "quality_rejection"
        doc["timestamp"] = time.time()
        doc["references"] = {"task_id": "test123"}
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.gate_results.get(3), "pass")

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_gate3_fail_known_type_invalid_schema(self, mock_verify):
        """Known type with invalid schema fails gate 3."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("credit_note")
        doc["type"] = "knarr/commerce/credit_note"
        # Missing required fields — should fail validation
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.gate_results.get(3), "fail")
        self.assertIn("Gate 3 failed", result.reason)

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_gate3_unknown_type_triggers_hold(self, mock_verify):
        """Unknown document type defaults to hold_for_review."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("totally_new_type")
        result = wm.ingest(doc, FAKE_PUBKEY)
        # Unknown type should still pass gate 3 (it's not a schema fail)
        # but action should be hold_for_review via _default rule
        self.assertEqual(result.status, "held")
        self.assertEqual(result.gate_results.get(3), "pass")

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_gate3_pass_recognized_with_valid_body(self, mock_verify):
        """Recognized type with valid body fields passes gate 3."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("payment_received")
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.gate_results.get(3), "pass")


# ---------- Gate 4: Integrity ----------

class TestGate4Integrity(unittest.TestCase):
    """Gate 4: proof fields valid, timestamp sane."""

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_gate4_pass_valid_proof(self, mock_verify):
        """Valid proof fields pass gate 4."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("payment_received")
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.gate_results.get(4), "pass")

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_gate4_fail_missing_proof(self, mock_verify):
        """Document without proof object fails gate 4."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("payment_received")
        del doc["proof"]
        # Gate 1 will also try to verify but we mock that.
        # Gate 4 should fail on missing proof.
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.gate_results.get(4), "fail")

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_gate4_fail_future_timestamp(self, mock_verify):
        """Timestamp too far in the future fails gate 4."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("payment_received")
        # Set created to 2 hours in future
        from datetime import datetime, timezone, timedelta
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        doc["proof"]["created"] = future.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.gate_results.get(4), "fail")
        self.assertIn("future", result.reason)

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_gate4_fail_old_timestamp(self, mock_verify):
        """Timestamp older than 7 days fails gate 4."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("payment_received")
        doc["proof"]["created"] = "2020-01-01T00:00:00.000Z"
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.gate_results.get(4), "fail")
        self.assertIn("old", result.reason)


# ---------- Gate 5: Authorization ----------

class TestGate5Authorization(unittest.TestCase):
    """Gate 5: signer has authority for the document type."""

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_gate5_pass_authorized_signer(self, mock_verify):
        """configuration_order signed by #cockpit-1 passes gate 5."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc(
            "configuration_order",
            vm=f"did:knarr:{NODE_ID}#cockpit-1",
        )
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.gate_results.get(5), "pass")

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_gate5_fail_unauthorized_signer(self, mock_verify):
        """configuration_order signed by #key-1 (not #cockpit-1) fails gate 5."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc(
            "configuration_order",
            vm=f"did:knarr:{'b' * 64}#key-1",
        )
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.gate_results.get(5), "fail")
        self.assertIn("Gate 5 failed", result.reason)


# ---------- Lifecycle: auto_promote ----------

class TestAutoPromoteLifecycle(unittest.TestCase):
    """auto_promote: pass all gates -> emit to bus + write receipt."""

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_auto_promote_emits_and_writes(self, mock_verify):
        """Auto-promoted document emits bus event and writes receipt."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("credit_note")
        doc["type"] = "knarr/commerce/credit_note"
        doc["amount"] = 2.0
        doc["reason"] = "partial_refund"
        doc["timestamp"] = time.time()
        doc["references"] = {"task_id": "task_xyz"}
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "promoted")
        self.assertIsNone(result.quarantine_id)
        # Bus emit called
        bus.emit.assert_called_once()
        call_args = bus.emit.call_args
        self.assertIn("wm.promoted.credit_note", call_args[0])
        # write_receipt callback called
        wr.assert_called_once()


# ---------- Lifecycle: hold_for_review ----------

class TestHoldForReviewLifecycle(unittest.TestCase):
    """hold_for_review: pass gates -> held -> approve/reject."""

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_hold_then_approve(self, mock_verify):
        """Held document can be approved and promoted."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc(
            "settlement_prepared",
            vm=f"did:knarr:{NODE_ID}#cockpit-1",
        )
        doc["type"] = "knarr/commerce/settle_request"
        doc["current_balance"] = 10.0
        doc["credit_limit"] = 5.0
        doc["provider_wallet"] = "A" * 44
        doc["timestamp"] = time.time()
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "held")
        self.assertIsNotNone(result.quarantine_id)

        # Approve
        ok = wm.approve(result.quarantine_id)
        self.assertTrue(ok)
        # Verify promoted_at is set
        row = st.quarantine_get(result.quarantine_id)
        self.assertEqual(row["status"], "promoted")
        self.assertIsNotNone(row["promoted_at"])
        # Bus emitted twice (held + promoted)
        self.assertEqual(bus.emit.call_count, 2)
        # write_receipt called on approve
        wr.assert_called_once()

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_hold_then_reject(self, mock_verify):
        """Held document can be rejected with reason."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc(
            "settlement_prepared",
            vm=f"did:knarr:{NODE_ID}#cockpit-1",
        )
        doc["type"] = "knarr/commerce/settle_request"
        doc["current_balance"] = 10.0
        doc["credit_limit"] = 5.0
        doc["provider_wallet"] = "A" * 44
        doc["timestamp"] = time.time()
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "held")

        # Reject
        ok = wm.reject(result.quarantine_id, "operator says no")
        self.assertTrue(ok)
        row = st.quarantine_get(result.quarantine_id)
        self.assertEqual(row["status"], "rejected")
        self.assertIsNotNone(row["resolved_at"])
        self.assertIn("operator says no", row["reason"])
        # write_receipt NOT called (rejected, not promoted)
        wr.assert_not_called()

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_request_review_returns_document(self, mock_verify):
        """request_review returns the quarantined document dict."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc(
            "settlement_prepared",
            vm=f"did:knarr:{NODE_ID}#cockpit-1",
        )
        doc["type"] = "knarr/commerce/settle_request"
        doc["current_balance"] = 10.0
        doc["credit_limit"] = 5.0
        doc["provider_wallet"] = "A" * 44
        doc["timestamp"] = time.time()
        result = wm.ingest(doc, FAKE_PUBKEY)
        reviewed = wm.request_review(result.quarantine_id)
        self.assertIsNotNone(reviewed)
        self.assertEqual(reviewed["document_type"], "settlement_prepared")

    def test_request_review_nonexistent_returns_none(self):
        """request_review on bogus ID returns None."""
        wm, bus, st, wr = _make_wm()
        self.assertIsNone(wm.request_review("bogus_id"))


# ---------- Restart recovery ----------

class TestRestartRecovery(unittest.TestCase):
    """Restart recovery: pending and approved-not-promoted items re-processed."""

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_pending_items_exist_after_ingest(self, mock_verify):
        """Items held as pending are visible in quarantine_list_pending."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc(
            "settlement_prepared",
            vm=f"did:knarr:{NODE_ID}#cockpit-1",
        )
        doc["type"] = "knarr/commerce/settle_request"
        doc["current_balance"] = 10.0
        doc["credit_limit"] = 5.0
        doc["provider_wallet"] = "A" * 44
        doc["timestamp"] = time.time()
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "held")

        pending = st.quarantine_list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], result.quarantine_id)

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_approved_not_promoted_visible(self, mock_verify):
        """Approved items without promoted_at are visible for recovery."""
        st = _QuarantineStorage()
        wm, bus, _, wr = _make_wm(storage=st)

        # Manually insert an approved-but-not-promoted record
        doc = _make_signed_doc("credit_note")
        doc["type"] = "knarr/commerce/credit_note"
        doc["amount"] = 1.0
        doc["reason"] = "other"
        doc["timestamp"] = time.time()
        doc["references"] = {"task_id": "t1"}
        doc_json = json.dumps(doc, sort_keys=True, separators=(",", ":"))
        st.quarantine_store(
            id="dmz_recovery_test",
            document_type="credit_note",
            document_json=doc_json,
            originator_pubkey=FAKE_PUBKEY.hex(),
            status="approved",
            gate_results="{}",
            reason=None,
        )

        approved = st.quarantine_list_by_status("approved")
        self.assertEqual(len(approved), 1)
        # promoted_at is None — should be picked up by restart recovery
        self.assertIsNone(approved[0]["promoted_at"])


# ---------- Config rules ----------

class TestConfigRules(unittest.TestCase):
    """Config rule loading and per-type override."""

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_default_rule_for_unknown_type(self, mock_verify):
        """Unknown document type uses _default rule -> hold_for_review."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("some_new_thing")
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "held")

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_config_override_respected(self, mock_verify):
        """Per-type config override changes action."""
        config = {
            "debug": True,
            "rules": {
                "credit_note": {"gates": [1, 2, 3, 4], "action": "hold_for_review"},
            },
        }
        wm, bus, st, wr = _make_wm(config_override=config)
        doc = _make_signed_doc("credit_note")
        doc["type"] = "knarr/commerce/credit_note"
        doc["amount"] = 1.0
        doc["reason"] = "other"
        doc["timestamp"] = time.time()
        doc["references"] = {"task_id": "t1"}
        result = wm.ingest(doc, FAKE_PUBKEY)
        # Default action for credit_note is auto_promote, but config overrides to hold
        self.assertEqual(result.status, "held")

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_reject_action_from_config(self, mock_verify):
        """Config action='reject' discards the document."""
        config = {
            "debug": True,
            "rules": {
                "credit_note": {"gates": [1, 2, 3, 4], "action": "reject"},
            },
        }
        wm, bus, st, wr = _make_wm(config_override=config)
        doc = _make_signed_doc("credit_note")
        doc["type"] = "knarr/commerce/credit_note"
        doc["amount"] = 1.0
        doc["reason"] = "other"
        doc["timestamp"] = time.time()
        doc["references"] = {"task_id": "t1"}
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "rejected")
        self.assertIn("Rejected by rule", result.reason)


# ---------- IngestResult dataclass ----------

class TestIngestResult(unittest.TestCase):
    """IngestResult is a frozen dataclass."""

    def test_frozen(self):
        from knarr.core.warehouse_manager import IngestResult
        r = IngestResult(
            status="promoted",
            document_type="credit_note",
            quarantine_id=None,
            gate_results={1: "pass"},
            reason=None,
        )
        with self.assertRaises(AttributeError):
            r.status = "hacked"

    def test_fields(self):
        from knarr.core.warehouse_manager import IngestResult
        r = IngestResult(
            status="held",
            document_type="test",
            quarantine_id="dmz_abc",
            gate_results={1: "pass", 2: "fail"},
            reason="bad address",
        )
        self.assertEqual(r.status, "held")
        self.assertEqual(r.document_type, "test")
        self.assertEqual(r.quarantine_id, "dmz_abc")
        self.assertEqual(r.gate_results[2], "fail")
        self.assertEqual(r.reason, "bad address")


# ---------- Edge cases ----------

class TestEdgeCases(unittest.TestCase):
    """Edge cases and boundary conditions."""

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_approve_nonexistent_returns_false(self, mock_verify):
        """Approving a non-existent quarantine ID returns False."""
        wm, bus, st, wr = _make_wm()
        self.assertFalse(wm.approve("nonexistent"))

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_reject_nonexistent_returns_false(self, mock_verify):
        """Rejecting a non-existent quarantine ID returns False."""
        wm, bus, st, wr = _make_wm()
        self.assertFalse(wm.reject("nonexistent", "reason"))

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_double_approve_fails(self, mock_verify):
        """Cannot approve an already-promoted item."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc(
            "settlement_prepared",
            vm=f"did:knarr:{NODE_ID}#cockpit-1",
        )
        doc["type"] = "knarr/commerce/settle_request"
        doc["current_balance"] = 10.0
        doc["credit_limit"] = 5.0
        doc["provider_wallet"] = "A" * 44
        doc["timestamp"] = time.time()
        result = wm.ingest(doc, FAKE_PUBKEY)
        wm.approve(result.quarantine_id)
        # Second approve should still succeed (status is now "promoted" which is valid for approve check)
        # Actually, once status is "promoted", it's no longer in "pending"/"approved" so it returns False
        row = st.quarantine_get(result.quarantine_id)
        self.assertEqual(row["status"], "promoted")
        ok = wm.approve(result.quarantine_id)
        self.assertFalse(ok)

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_reject_already_rejected_fails(self, mock_verify):
        """Cannot reject an already-rejected item."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc(
            "settlement_prepared",
            vm=f"did:knarr:{NODE_ID}#cockpit-1",
        )
        doc["type"] = "knarr/commerce/settle_request"
        doc["current_balance"] = 10.0
        doc["credit_limit"] = 5.0
        doc["provider_wallet"] = "A" * 44
        doc["timestamp"] = time.time()
        result = wm.ingest(doc, FAKE_PUBKEY)
        wm.reject(result.quarantine_id, "first reject")
        ok = wm.reject(result.quarantine_id, "second reject")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
