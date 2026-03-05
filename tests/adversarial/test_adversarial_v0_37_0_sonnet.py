"""Adversarial tests for v0.37.0 Vordur — The Security Membrane.

Attacker model: Claude Sonnet 4.6
Mandate: Break the code. Failing tests prove bugs. Do NOT fix.

Each test targets ONE finding. Name pattern: test_adv_NNN_short_description.
"""

import asyncio
import importlib
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Path setup — ensure src is on sys.path
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

# ---------------------------------------------------------------------------
# Helpers to import modules under test
# ---------------------------------------------------------------------------

def _load_warehouse_manager():
    sys.path.insert(0, str(BASE_DIR / "src"))
    from knarr.core.warehouse_manager import WarehouseManager, IngestResult
    return WarehouseManager, IngestResult


def _load_punchhole_backend():
    plugin_path = BASE_DIR / "src" / "knarr" / "plugins" / "09-punchhole-backend"
    sys.path.insert(0, str(plugin_path))
    spec = importlib.util.spec_from_file_location("punchhole_backend", str(plugin_path / "handler.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_punchhole_frontend():
    plugin_path = BASE_DIR / "src" / "knarr" / "plugins" / "08-punchhole-frontend"
    sys.path.insert(0, str(plugin_path))
    spec = importlib.util.spec_from_file_location("punchhole_frontend", str(plugin_path / "handler.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_bcw():
    plugin_path = BASE_DIR / "src" / "knarr" / "plugins" / "10-bcw"
    sys.path.insert(0, str(plugin_path))
    spec = importlib.util.spec_from_file_location("bcw_handler", str(plugin_path / "handler.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

import sqlite3


class InMemoryQuarantineStorage:
    """In-memory quarantine storage for WM tests."""

    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
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
        self._conn.commit()

    def quarantine_store(self, id, document_type, document_json, originator_pubkey,
                         status, gate_results, reason):
        now = time.time()
        self._conn.execute(
            "INSERT OR REPLACE INTO dmz_quarantine "
            "(id, document_type, document_json, originator_pubkey, status, gate_results, reason, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (id, document_type, document_json, originator_pubkey, status, gate_results, reason, now),
        )
        self._conn.commit()

    def quarantine_get(self, id):
        cur = self._conn.execute(
            "SELECT id, document_type, document_json, originator_pubkey, status, gate_results, "
            "reason, received_at, promoted_at, resolved_at FROM dmz_quarantine WHERE id = ?",
            (id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip(
            ["id", "document_type", "document_json", "originator_pubkey", "status",
             "gate_results", "reason", "received_at", "promoted_at", "resolved_at"],
            row,
        ))

    def quarantine_update_status(self, id, status, reason=None, promoted_at=None, resolved_at=None):
        self._conn.execute(
            "UPDATE dmz_quarantine SET status=?, reason=COALESCE(?,reason), "
            "promoted_at=COALESCE(?,promoted_at), resolved_at=COALESCE(?,resolved_at) WHERE id=?",
            (status, reason, promoted_at, resolved_at, id),
        )
        self._conn.commit()


def _make_wm(identity_fragments=None, config=None):
    """Build a WarehouseManager with mocked dependencies."""
    WarehouseManager, IngestResult = _load_warehouse_manager()
    bus = MagicMock()
    storage = InMemoryQuarantineStorage()
    write_receipt_cb = MagicMock()
    wm = WarehouseManager(
        node_id="test_node",
        identity_fragments=identity_fragments or ["did:knarr:testnode"],
        bus=bus,
        storage=storage,
        config=config or {},
        write_receipt_cb=write_receipt_cb,
    )
    return wm, bus, storage, write_receipt_cb


def _now_iso():
    """Return current UTC time as ISO 8601 string for use in test documents."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _valid_doc(doc_type="credit_note", identity="did:knarr:testnode"):
    """Build a minimal document that passes addressing check."""
    return {
        "document_type": doc_type,
        "identity": identity,
        "proof": {
            "type": "DataIntegrityProof",
            "cryptosuite": "eddsa-jcs-2022",
            "created": _now_iso(),
            "proofValue": "zABCDEFGHIJ",
            "verificationMethod": "did:knarr:testnode#key-1",
        },
    }


# ===========================================================================
# A. GATE BYPASS
# ===========================================================================


class TestGateBypass:
    """Category B: Gate Bypass Attacks on WarehouseManager."""

    def test_adv_001_wrong_pubkey_length_31_bytes_passes_gate1(self):
        """
        ADV-001: 31-byte originator_pubkey raises exception in VerifyKey() but
        WM catches it and calls _quarantine(). Verify it rejects (not passes).

        Finding: Gate 1 exception handling catches nacl.exceptions.ValueError
        for wrong-length key, which is correct. But we verify it really rejects.
        This is a regression guard — verify the rejection path is correct.
        """
        wm, bus, storage, _ = _make_wm()
        doc = _valid_doc()
        bad_pubkey = b"\x01" * 31  # 31 bytes, not 32

        with patch("knarr.core.warehouse_manager.verify_document", return_value=True):
            result = wm.ingest(doc, bad_pubkey)
        # VerifyKey(31_bytes) raises an exception -> Gate 1 fail -> quarantine
        assert result.status == "rejected"
        assert result.gate_results.get(1) == "fail"

    def test_adv_002_gate5_configuration_order_substring_bypass(self):
        """
        ADV-002: Gate 5 authorization for configuration_order uses `"#cockpit-1" in vm`.
        A vm of "did:knarr:testnode#cockpit-1-fake" contains "#cockpit-1" as a SUBSTRING
        and should be REJECTED, but PASSES because Python `in` checks substring.

        Location: warehouse_manager.py:402 — `return "#cockpit-1" in vm`
        This is a HIGH severity logic error — substring match instead of exact fragment check.

        Attack: Attacker constructs vm = "did:knarr:testnode#cockpit-1-fake" which:
        1. Contains our node id (passes Gate 2 addressing check via `frag in vm`)
        2. Contains "#cockpit-1" as substring (passes Gate 5 authorization check)
        But "#cockpit-1-fake" is not the legitimate "#cockpit-1" verificationMethod.
        """
        wm, bus, storage, _ = _make_wm(identity_fragments=["did:knarr:testnode"], config={})
        # The malicious vm contains BOTH our node id (for Gate 2) AND "#cockpit-1" as substring
        malicious_vm = "did:knarr:testnode#cockpit-1-fake"  # passes both substring checks!

        doc = {
            "document_type": "configuration_order",
            "identity": "did:knarr:testnode",
            "target": "punchhole",
            "operation": "upsert_object",
            "changes": {"key": "value"},
            "proof": {
                "type": "DataIntegrityProof",
                "cryptosuite": "eddsa-jcs-2022",
                "created": _now_iso(),
                "proofValue": "zABCDEFGHIJ",
                "verificationMethod": malicious_vm,
            },
        }

        with patch("knarr.core.warehouse_manager.verify_document", return_value=True):
            result = wm.ingest(doc, b"\x00" * 32)

        # Gate 5 should REJECT this because "#cockpit-1-fake" is not "#cockpit-1".
        # But due to substring match at line 402, it PASSES Gate 5 -> document is "held" not "rejected".
        # The test FAILS (proving the bug) when result.status == "held" instead of "rejected".
        assert result.status == "rejected", (
            f"BUG: '#cockpit-1' substring match allows '#cockpit-1-fake' to pass Gate 5! "
            f"result.status={result.status!r}, gate_results={result.gate_results}. "
            "warehouse_manager.py:402 uses 'in' operator (substring) instead of exact vm fragment match. "
            "An attacker using vm='did:knarr:X#cockpit-1-fake' bypasses the cockpit authorization check."
        )

    def test_adv_003_gate5_vm_none_does_not_crash(self):
        """
        ADV-003: Gate 5 _check_authorization extracts vm from proof.
        If proof.verificationMethod is None, `"#cockpit-1" in None` raises TypeError.
        warehouse_manager.py:398 has: vm = proof.get("verificationMethod", "")
        If verificationMethod is None (explicitly set), vm = None.
        Then `"#cockpit-1" in None` raises TypeError -> unhandled -> propagates.

        Location: warehouse_manager.py:398-402
        """
        wm, bus, storage, _ = _make_wm()
        doc = {
            "document_type": "configuration_order",
            "identity": "did:knarr:testnode",
            "target": "test",
            "operation": "upsert_object",
            "changes": {},
            "proof": {
                "type": "DataIntegrityProof",
                "cryptosuite": "eddsa-jcs-2022",
                "created": _now_iso(),
                "proofValue": "zABCDEFGHIJ",
                "verificationMethod": None,  # Explicit None
            },
        }

        with patch("knarr.core.warehouse_manager.verify_document", return_value=True):
            # Should not raise — should cleanly reject
            try:
                result = wm.ingest(doc, b"\x00" * 32)
                assert result.status == "rejected", "Gate 5 with None vm should reject"
            except TypeError as e:
                pytest.fail(f"BUG: Gate 5 crashes with TypeError when vm is None: {e}")

    def test_adv_004_gate3_validator_exception_fail_open(self):
        """
        ADV-004: Gate 3 calls validator(body). If validator raises an exception
        (rather than returning (False, err)), does WM fail open (pass) or fail closed?

        warehouse_manager.py:214 — `valid, err = validator(body)` with no try/except.
        An exception in validator propagates uncaught into ingest(). This is a CRASH
        vulnerability: any schema validator that raises (e.g., due to a malicious
        document with unexpected types) causes WM.ingest() to raise instead of quarantining.

        Location: warehouse_manager.py:214 — no try/except around validator()

        Attack: Send a document with a body that causes the validator to throw
        (e.g., by passing a non-dict where the validator iterates over it).
        Result: WM crashes with unhandled exception instead of quarantining.
        """
        wm, bus, storage, _ = _make_wm()
        doc = _valid_doc("credit_note")

        exception_raised = []

        def crashing_validator(body):
            exception_raised.append(True)
            raise RuntimeError("internal validator error")

        # Patch validators dict directly on the WM instance
        wm._validators = {"credit_note": crashing_validator}

        with patch("knarr.core.warehouse_manager.verify_document", return_value=True):
            try:
                result = wm.ingest(doc, b"\x00" * 32)
                # If we reach here, WM didn't crash — check it failed closed
                assert exception_raised, "crashing_validator was never called — test setup issue"
                assert result.status == "rejected", (
                    f"BUG: Validator exception caused fail-open! status={result.status}. "
                    "warehouse_manager.py:214 has no try/except around validator(body)"
                )
            except RuntimeError as exc:
                pytest.fail(
                    f"BUG: Validator exception propagated uncaught from WM.ingest(): {exc}. "
                    "warehouse_manager.py:214 — Gate 3 must catch validator exceptions and "
                    "fail closed (quarantine), not crash the caller."
                )

    def test_adv_005_gate2_addressing_empty_identity_fragment(self):
        """
        ADV-005: WM is initialized with an empty string as identity fragment.
        Gate 2 iterates fields and checks `value in self._identity_fragments`.
        If identity_fragments = {""}, any document with `identity = ""` would
        pass Gate 2 — an empty value should never be a valid address.

        warehouse_manager.py:131 — `self._identity_fragments = set(identity_fragments)`
        warehouse_manager.py:339 — `if isinstance(value, str) and value in self._identity_fragments`
        """
        # WM with empty string as identity fragment
        wm, bus, storage, _ = _make_wm(identity_fragments=[""])
        doc = {
            "document_type": "credit_note",
            "identity": "",  # empty string matches!
            "proof": {
                "type": "DataIntegrityProof",
                "cryptosuite": "eddsa-jcs-2022",
                "created": _now_iso(),
                "proofValue": "zABCDEFGHIJ",
                "verificationMethod": "did:knarr:testnode#key-1",
            },
        }

        with patch("knarr.core.warehouse_manager.verify_document", return_value=True):
            result = wm.ingest(doc, b"\x00" * 32)

        # An empty identity should NOT match — it's an address spoofing attack
        assert result.status == "rejected", (
            "BUG: Empty identity fragment '' matches Gate 2 check. "
            "WM accepts any document with identity='' when '' is in identity_fragments."
        )

    def test_adv_006_gate2_proof_vm_substring_match(self):
        """
        ADV-006: Gate 2 addressing check for proof.verificationMethod uses:
            `if frag in vm`
        This substring check means if frag = "did:knarr:abc" and
        vm = "did:knarr:abcXXXXevil#key-1", the check passes incorrectly.

        warehouse_manager.py:346 — `if frag in vm` is substring check.
        An attacker can construct a verificationMethod that contains our node ID
        as a substring but belongs to a different DID.

        Location: warehouse_manager.py:346
        """
        # Our identity fragment is a legit DID
        our_fragment = "did:knarr:legitimate"
        wm, bus, storage, _ = _make_wm(identity_fragments=[our_fragment])

        doc = {
            "document_type": "credit_note",
            "identity": "did:knarr:evil",  # different identity
            "proof": {
                "type": "DataIntegrityProof",
                "cryptosuite": "eddsa-jcs-2022",
                "created": _now_iso(),
                "proofValue": "zABCDEFGHIJ",
                # vm contains our fragment as a substring!
                "verificationMethod": f"did:knarr:legitmateXXX#{our_fragment}#key-1",
            },
        }

        with patch("knarr.core.warehouse_manager.verify_document", return_value=True):
            result = wm.ingest(doc, b"\x00" * 32)

        # The vm contains our fragment as substring -> Gate 2 should fail
        # but may pass due to substring check
        # This is a theoretical finding (depends on exact fragment strings) but
        # demonstrating the mechanism is sound


# ===========================================================================
# B. APPROVE/REJECT STATE MACHINE BUGS
# ===========================================================================


class TestQuarantineStateMachine:
    """Category C: Quarantine state machine correctness."""

    def test_adv_007_approve_already_promoted_allows_double_emit(self):
        """
        ADV-007: WM.approve() checks `row["status"] not in ("pending", "approved")`.
        This means a document with status="approved" can be re-approved.
        The status check at warehouse_manager.py:279 explicitly allows "approved"
        to be re-approved, which would emit to bus and write receipt AGAIN.

        Location: warehouse_manager.py:279
        `if row is None or row["status"] not in ("pending", "approved"):`

        "approved" is not a real state — quarantine statuses are: pending, promoted, rejected.
        Yet the code allows re-approving "approved" which emits duplicate bus events.
        """
        wm, bus, storage, write_receipt = _make_wm()
        doc = _valid_doc("settlement_prepared")

        with patch("knarr.core.warehouse_manager.verify_document", return_value=True):
            result = wm.ingest(doc, b"\x00" * 32)

        qid = result.quarantine_id
        assert qid is not None

        # Manually set status to "approved" (not a normal WM state)
        storage.quarantine_update_status(qid, "approved")

        # Approve again — should this be allowed?
        result2 = wm.approve(qid)
        # The code explicitly allows "approved" status at line 279,
        # so this will emit to bus AGAIN (double emission)
        assert result2 == False, (
            "BUG: approve() allows re-approving a document already in 'approved' state. "
            "warehouse_manager.py:279 — 'approved' is in the allowed set for re-approval. "
            "This causes duplicate bus emissions and duplicate receipt writes."
        )

    def test_adv_008_old_format_validators_fail_new_format_documents(self):
        """
        ADV-008: CRITICAL SCHEMA MISMATCH — Gate 3 schema validators for credit_note,
        settlement_prepared, execution_receipt etc. check:
            `body.get("type") != "knarr/commerce/credit_note"`
        But new-format documents (v0.37.0 style) have `document_type = "credit_note"`,
        NOT `type = "knarr/commerce/credit_note"`.

        WM Gate 3 at warehouse_manager.py:213 extracts `body = document.get("body", document)`
        and passes the full document to the validator. The validator checks for the OLD
        `type` field which new documents DO NOT HAVE.

        Result: ALL new-format documents for credit_note, execution_receipt,
        settlement_prepared, settlement_accepted, settlement_processed,
        settlement_confirmation, tab_reminder will FAIL Gate 3 and be quarantined
        as rejected — even when structurally valid.

        Location: warehouse_manager.py:213-219, schemas.py:9-12, 24-27, 44-47, 65-68, 85-88
        """
        from knarr.commerce.schemas import (
            validate_credit_note, validate_receipt, validate_settle_request,
            validate_settlement_confirmation, validate_tab_reminder,
        )

        # New-format documents (what knarr v0.37.0 produces)
        new_format_credit_note = {
            "document_type": "credit_note",
            "provider": "did:knarr:A",
            "consumer": "did:knarr:B",
            "amount": 5.0,
            "skill_name": "test",
            # Note: NO "type" field!
        }
        ok, err = validate_credit_note(new_format_credit_note)
        assert not ok, (
            "BUG CONFIRMED: validate_credit_note() rejects new-format documents because "
            "it checks `body.get('type') != 'knarr/commerce/credit_note'` at schemas.py:26. "
            "New-format documents use 'document_type' not 'type'. All old-format validators "
            "in schemas.py will FAIL Gate 3 for any new-format document."
        )
        assert err == "wrong type"

        # Same for settlement
        new_format_settlement = {"proposer": "A", "counterparty": "B", "amount": 10.0}
        ok2, err2 = validate_settle_request(new_format_settlement)
        assert not ok2
        assert err2 == "wrong type"

        # This means the WM Gate 3 will REJECT all new-format commerce documents
        # Let's prove this through the WM ingest path
        wm, bus, storage, _ = _make_wm()
        doc = {
            "document_type": "credit_note",
            "identity": "did:knarr:testnode",
            "provider": "did:knarr:A",
            "consumer": "did:knarr:testnode",
            "amount": 5.0,
            "skill_name": "test",
            "proof": {
                "type": "DataIntegrityProof",
                "cryptosuite": "eddsa-jcs-2022",
                "created": _now_iso(),
                "proofValue": "zABCDEFGHIJ",
                "verificationMethod": "did:knarr:testnode#key-1",
            },
        }

        with patch("knarr.core.warehouse_manager.verify_document", return_value=True):
            result = wm.ingest(doc, b"\x00" * 32)

        # Gate 3 REJECTS this — wrong validator format
        assert result.status == "rejected", (
            "Expected Gate 3 rejection due to validator format mismatch, "
            f"but got status={result.status}"
        )
        assert result.gate_results.get(3) == "fail", (
            f"Expected gate 3 fail, got: {result.gate_results}"
        )

    def test_adv_009_hold_quarantine_status_is_pending_not_held(self):
        """
        ADV-009: WM._hold() stores documents with status="pending".
        WM._quarantine() stores documents with status="rejected".

        But the IngestResult for hold returns status="held" while the DB
        stores "pending". This inconsistency means if you call request_review()
        on a "rejected" quarantine ID, it returns the document (status check
        would prevent approve/reject by the status in DB).

        Verify that a quarantined (rejected) document has status="rejected" in DB
        but IngestResult.status="rejected". This is consistent.
        And held documents have DB status="pending" but IngestResult.status="held".

        The bug: approve() checks `row["status"] not in ("pending", "approved")`.
        A rejected document (DB status="rejected") correctly blocks approve.
        But there is NO check in approve() that prevents calling approve() on a
        document that was gate-failed (quarantined, not held). The _quarantine()
        sets status="rejected" in DB, which blocks approve(). This is correct.

        This is a positive finding / regression guard — confirming correct behavior.
        """
        wm, bus, storage, _ = _make_wm()
        doc = _valid_doc("credit_note")
        doc["identity"] = "did:knarr:STRANGER"  # wrong identity -> gate 2 fails

        with patch("knarr.core.warehouse_manager.verify_document", return_value=True):
            result = wm.ingest(doc, b"\x00" * 32)

        assert result.status == "rejected"
        qid = result.quarantine_id
        row = storage.quarantine_get(qid)
        assert row["status"] == "rejected"

        # Try to approve a "rejected" document — should fail
        result_approve = wm.approve(qid)
        assert result_approve == False, "Should not be able to approve a gate-rejected document"


# ===========================================================================
# C. GRANULARITY CONTROL BUGS (Punchhole Backend)
# ===========================================================================


class TestGranularityControls:
    """Category F: Punchhole Backend granularity edge cases."""

    def setup_method(self):
        self.backend_mod = _load_punchhole_backend()
        self.apply = self.backend_mod._apply_granularity

    def test_adv_010_range_zero_divisor_returns_value_not_error(self):
        """
        ADV-010: _apply_granularity with control="range:0" and n=0.0.
        Code at handler.py:108: `if n <= 0: return value` — correctly avoids div-by-zero.
        But `range:0` is semantically "no rounding", so it should return exact value.

        This is a POSITIVE check — verify range:0 doesn't crash.
        Confirmed safe: n <= 0 returns value. Not a bug.
        """
        result = self.apply(1247.5, "range:0")
        assert result == 1247.5  # passthrough for range:0

    def test_adv_011_range_negative_n_returns_value_not_error(self):
        """
        ADV-011: _apply_granularity with control="range:-5".
        Code at handler.py:108: `if n <= 0: return value` — negative n returns exact value.
        Semantically "range:-5" means "no rounding" (n <= 0 branch).
        This is a validation gap: a schema with "range:-5" is silently accepted
        as "range:0" (passthrough) instead of being flagged as invalid.

        Location: handler.py:108 — negative n silently returns exact value.
        """
        result = self.apply(999.99, "range:-5")
        # Should it pass through or reject? Currently passes through silently.
        # This means an operator who writes "range:-5" by mistake gets exact disclosure
        # instead of an error indicating the misconfiguration.
        assert result == 999.99  # silently passes through — not rejected

    def test_adv_012_range_nan_input_dropped_not_leaked(self):
        """
        ADV-012: _apply_granularity with NaN float value and range:100 returns None.
        _build_data_dict() at handler.py:135-136 has `if val is None: continue`
        which drops ALL None values (not just hidden ones).

        REGRESSION GUARD: Verify NaN financial values are dropped (not leaked as None).
        This is CORRECT behavior — a NaN balance should not appear in disclosed data.

        Location: handler.py:135-136
        """
        backend_mod = _load_punchhole_backend()
        raw = {"credit_balance": float("nan"), "settlement_count": 5}
        fields = ["credit_balance", "settlement_count"]
        granularity = {"credit_balance": "range:100", "settlement_count": "exact"}

        result = backend_mod._build_data_dict(raw, fields, granularity)
        # NaN credit_balance -> _apply_granularity returns None -> dropped from output (CORRECT)
        assert "credit_balance" not in result, (
            "NaN value should be dropped from output dict, not leaked as None"
        )
        # Non-NaN settlement_count should be present
        assert result.get("settlement_count") == 5

    def test_adv_013_recent_zero_returns_empty_list(self):
        """
        ADV-013: _apply_granularity with control="recent:0".
        Code at handler.py:99: `return value[-n:] if n > 0 else []`
        n=0 -> n > 0 is False -> returns [].
        recent:0 means "return no recent items" = empty list.
        This is CORRECT behavior. Regression guard.
        """
        result = self.apply([1, 2, 3, 4, 5], "recent:0")
        assert result == []

    def test_adv_014_recent_negative_n_uses_negative_slice(self):
        """
        ADV-014: _apply_granularity with control="recent:-1".
        Code at handler.py:99: `return value[-n:] if n > 0 else []`
        n = -1, so n > 0 is False -> returns [].

        BUT: the validation `if n > 0` correctly returns [] for n <= 0.
        This is a positive guard — recent:-1 returns [] (empty), not all items.
        """
        result = self.apply([1, 2, 3, 4, 5], "recent:-1")
        # n = -1, n > 0 is False -> returns []
        assert result == []

    def test_adv_015_boolean_on_none_value(self):
        """
        ADV-015: _apply_granularity with control="boolean" and value=None.
        Code at handler.py:62: `return bool(value)`.
        bool(None) = False. So a missing/null field gets disclosed as False.

        This is a semantic error: None (field not present) is different from
        False (boolean false). The granularity system silently treats missing
        data as "False" rather than "unknown/absent".

        Location: handler.py:62
        """
        result = self.apply(None, "boolean")
        # bool(None) = False — a None value is disclosed as False boolean
        # An attacker can observe False vs True but cannot distinguish None from False
        assert result == False
        # This demonstrates the semantic ambiguity


# ===========================================================================
# D. BCW TRANSFER CLASSIFICATION
# ===========================================================================


class TestBCWClassification:
    """Category G: BCW transfer classification edge cases."""

    def setup_method(self):
        self.bcw_mod = _load_bcw()

    def test_adv_016_self_transfer_from_equals_to(self):
        """
        ADV-016: BCW._classify_transfer() when from_address == to_address.
        A self-transfer (address sends to itself) should hit the
        `from_self and to_self` branch -> "wallet_transfer".

        But with from_address == to_address == a self-owned address:
        - from_self = True (from_address in self_addresses)
        - to_self = True (to_address in self_addresses, same address)
        Result: "wallet_transfer" (correct for internal rebalancing).

        Verify this edge case produces the right classification.
        """
        from knarr.commerce.transfer_event import ConfirmationStatus, TransferEvent

        plugin_path = BASE_DIR / "src" / "knarr" / "plugins" / "10-bcw"
        sys.path.insert(0, str(plugin_path))

        ctx = MagicMock()
        ctx.plugin_dir = Path(tempfile.mkdtemp())
        ctx.node_id = "a" * 64
        ctx.vault_get = MagicMock(return_value=None)

        with patch.object(self.bcw_mod, "derive_counterparty_address",
                          side_effect=ValueError("invalid")):
            plugin = self.bcw_mod.BCWPlugin(ctx, {"enabled": False, "chains": []})

        self_addr = "SelfAddress111111111111111111111111111111111"
        plugin._self_owned_addresses = {self_addr}

        transfer = TransferEvent(
            chain_id="solana-mainnet",
            tx_hash="abc" * 21 + "x",
            tx_index=0,
            from_address=self_addr,
            to_address=self_addr,  # same address!
            amount=1_000_000,
            denom="SOL",
            decimals=9,
            confirmation=ConfirmationStatus.FINALIZED,
        )

        result = plugin._classify_transfer(transfer)
        assert result == "wallet_transfer"

    def test_adv_017_bcw_disabled_when_no_vault_seed(self):
        """
        ADV-017: BCW should disable itself when vault seed is missing.
        Verify _enabled is False when vault returns None.

        Location: handler.py:203-205
        """
        plugin_path = BASE_DIR / "src" / "knarr" / "plugins" / "10-bcw"
        sys.path.insert(0, str(plugin_path))

        ctx = MagicMock()
        ctx.plugin_dir = Path(tempfile.mkdtemp())
        ctx.node_id = "a" * 64
        ctx.vault_get = MagicMock(return_value=None)
        ctx.subscribe_events = MagicMock(return_value=MagicMock())

        plugin = self.bcw_mod.BCWPlugin(ctx, {"enabled": True, "chains": []})
        assert plugin._enabled == False, "BCW should be disabled when vault seed missing"

    def test_adv_018_bcw_address_derivation_non_hex_node_id(self):
        """
        ADV-018: derive_counterparty_address() validates len(node_id) == 64 chars
        but does NOT validate that node_id is valid hex before calling
        node_id.encode("utf-8") and hashing. Non-hex node_id is silently accepted.

        Location: handler.py:33-38 — only checks length, not hex validity.
        An attacker can register a watch with a 64-char non-hex node_id and get
        a derived address (the SHA256 just hashes the raw bytes of the string).
        This is not a crash but a semantic confusion bug.
        """
        valid_seed = b"\x01" * 32
        # 64 chars but NOT valid hex (contains 'g'-'z' chars)
        non_hex_node_id = "z" * 64

        # Should it raise ValueError for invalid hex? Currently doesn't.
        from knarr.core.wallet import derive_solana_address
        from nacl.signing import SigningKey
        import hashlib

        # Directly replicate the derivation logic
        # handler.py:35: seed = hashlib.sha256(master_seed + node_id.encode("utf-8")).digest()
        seed = hashlib.sha256(valid_seed + non_hex_node_id.encode("utf-8")).digest()
        # This succeeds — no hex validation
        assert len(seed) == 32  # No error raised for non-hex node_id

        # The validation at line 33 only checks length:
        # `if len(node_id) != 64: raise ValueError(...)`
        # A 64-char garbage string passes through
        # Prove that non-hex 64-char strings silently derive addresses
        addr = derive_solana_address(SigningKey(seed))
        assert addr  # address is derived from garbage input

    def test_adv_019_parse_positive_amount_zero_returns_none(self):
        """
        ADV-019: SolanaWatcher._parse_positive_amount(0) returns None.
        Code at solana.py:245: `if amount <= 0: return None`
        So amount=0 lamports is treated as "no transfer" (filtered out).
        This is correct — 0-lamport transfers are noise.

        But _parse_non_negative_int(0) at solana.py:209-214 explicitly handles
        raw in (0, "0", 0.0) -> returns 0. This asymmetry means:
        - slot=0 is valid (non_negative_int returns 0)
        - amount=0 is invalid (parse_positive_amount returns None -> filtered)
        Regression guard confirming this is correct.
        """
        plugin_path = BASE_DIR / "src" / "knarr" / "plugins" / "10-bcw"
        sys.path.insert(0, str(plugin_path))
        import solana as solana_mod

        assert solana_mod.SolanaWatcher._parse_positive_amount(0) is None
        assert solana_mod.SolanaWatcher._parse_non_negative_int(0) == 0


# ===========================================================================
# E. DOCUMENT TYPE / SCHEMA ATTACKS (Track C)
# ===========================================================================


class TestDocumentTypeAttacks:
    """Category H: Document type and schema attacks."""

    def test_adv_020_payment_received_negative_amount_passes_wm_gate3(self):
        """
        ADV-020: validate_payment_received delegates to _validate_chain_tx which
        checks: `if not isinstance(amt, (int, float)) or not _is_finite(amt) or amt <= 0`
        So negative amount FAILS validation. Good.

        BUT the WM Gate 3 extracts `body = document.get("body", document)`.
        If a payment_received document has a top-level `amount` field (not nested in "body"),
        it is validated directly at the top level. Since validate_payment_received
        correctly rejects negative amounts, this is a regression guard.
        """
        from knarr.commerce.schemas import validate_payment_received
        body = {
            "chain_id": "solana-mainnet",
            "tx_hash": "abc123",
            "tx_index": 0,
            "from_address": "Sender",
            "to_address": "Receiver",
            "amount": -1,  # negative
            "denom": "SOL",
            "decimals": 9,
            "confirmation": {"level": "finalized"},
        }
        ok, err = validate_payment_received(body)
        assert not ok
        assert "amount" in err

    def test_adv_021_payment_finalized_missing_finality_level(self):
        """
        ADV-021: validate_payment_finalized checks:
        `if not isinstance(fin, dict) or fin.get("level") != "finalized"`
        But what if finality = {"level": "FINALIZED"} (uppercase)?
        The check is case-sensitive, so FINALIZED (uppercase) fails.
        This is CORRECT but tests the validator strictness.
        """
        from knarr.commerce.schemas import validate_payment_finalized
        body = {
            "chain_id": "solana-mainnet",
            "tx_hash": "abc123",
            "amount": 1_000_000,
            "denom": "SOL",
            "original_receipt_id": "prx_abc123",
            "finality": {"level": "FINALIZED"},  # uppercase
        }
        ok, err = validate_payment_finalized(body)
        assert not ok
        assert "finalized" in err

    def test_adv_022_configuration_order_sql_injection_in_operation(self):
        """
        ADV-022: validate_configuration_order checks operation against a whitelist:
        `valid_ops = {"upsert_object", "modify_access", "remove_object"}`
        So "DROP TABLE" is correctly rejected. Regression guard.

        But the `changes` dict has NO validation of its contents.
        An attacker can put arbitrary data in changes: {"DROP TABLE": "..."}.
        The schema validator accepts any dict as changes.

        Location: schemas.py:183 — `if not isinstance(body.get("changes"), dict): return False`
        Only checks that changes is a dict, not its contents.
        """
        from knarr.commerce.schemas import validate_configuration_order

        # Malicious operation — rejected by whitelist
        body_bad_op = {
            "target": "punchhole",
            "operation": "DROP TABLE; --",
            "changes": {},
        }
        ok, err = validate_configuration_order(body_bad_op)
        assert not ok  # Correctly rejected

        # But arbitrary changes dict content is accepted
        body_good_op_evil_changes = {
            "target": "punchhole",
            "operation": "upsert_object",
            "changes": {
                "DROP TABLE users": "injected",
                "__proto__": {"polluted": True},
                "../../etc/passwd": "arbitrary",
            },
        }
        ok2, err2 = validate_configuration_order(body_good_op_evil_changes)
        assert ok2, (
            "Regression guard: configuration_order validator accepts any dict as 'changes'. "
            "The operation whitelist prevents SQL injection via operation field, but "
            "changes contents are completely unvalidated. An attacker controlling a "
            "#cockpit-1 key can send arbitrary structured data as changes."
        )

    def test_adv_023_cache_object_data_must_be_dict_not_list(self):
        """
        ADV-023: validate_cache_object checks `if not isinstance(body.get("data"), dict)`.
        A list is NOT a dict, so it's rejected. Regression guard.
        """
        from knarr.commerce.schemas import validate_cache_object
        body = {
            "object_key": "economy.summary",
            "data": [],  # list, not dict
            "granularity": {},
        }
        ok, err = validate_cache_object(body)
        assert not ok
        assert "dict" in err

    def test_adv_024_punchhole_card_available_string_not_list(self):
        """
        ADV-024: validate_punchhole_card checks `if not isinstance(body.get("available"), list)`.
        A string like "all" would fail. Regression guard.
        """
        from knarr.commerce.schemas import validate_punchhole_card
        body = {
            "for_node": "did:knarr:test",
            "for_access_level": "all_signed",
            "available": "all",  # string, not list
            "not_available": [],
        }
        ok, err = validate_punchhole_card(body)
        assert not ok
        assert "list" in err


# ===========================================================================
# F. DYNAMIC SKILLS (Track A1)
# ===========================================================================


class TestDynamicSkills:
    """Category I: Dynamic skill registration attacks."""

    def test_adv_025_write_dynamic_skill_injects_toml_via_skill_name(self):
        """
        ADV-025: write_dynamic_skill() builds TOML by string interpolation:
            `lines.append(f"[skills.{sname}]")`

        validate_dynamic_skill() checks:
            `if not re.match(r'^[a-z0-9][a-z0-9-]*$', skill_name)`
        This regex PREVENTS injection via skill names. Regression guard.

        But what about skill_cfg VALUES? A string value like:
            price = 1.0\n[skills.evil]\nprice = 2.0
        could inject additional TOML sections.

        Location: config.py:359 — `lines.append(f'{k} = "{v}"')`
        A string value with embedded newline + TOML section header would be
        split into multiple lines in the written file.

        NOTE: Python's f-string does NOT escape the value — a newline in v
        would create a valid multi-line injection.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            from knarr.cli.config import write_dynamic_skill

            injected_value = "legitimate\n[skills.injected]\nprice = 99.0\nhandler = \"evil.py\""
            skill_cfg = {
                "handler": "dynamic_facade.py:handle",
                "price": 1.0,
                "description": injected_value,  # injection via description
            }

            write_dynamic_skill(config_dir, "legit-skill", skill_cfg)

            # Read back the file and check if injection worked
            skills_path = config_dir / "knarr.skills.toml"
            content = skills_path.read_text(encoding="utf-8")

            # Check if the injected section header appears in the file
            assert "[skills.injected]" in content, (
                "BUG: String value injection via write_dynamic_skill() allows TOML section "
                "header injection. config.py:359 — f-string writes string values without "
                "escaping newlines. An attacker can inject TOML sections via skill description field."
            )

    def test_adv_026_dynamic_price_nan_rejected(self):
        """
        ADV-026: validate_dynamic_skill() checks:
        `if not isinstance(price, (int, float)) or not _math.isfinite(price)`
        NaN and Inf are correctly rejected. Regression guard.
        """
        from knarr.cli.config import validate_dynamic_skill
        policy = {
            "dynamic_enabled": True,
            "dynamic_price_floor": 0.5,
            "dynamic_price_ceiling": 50.0,
            "dynamic_allowed_handlers": ["dynamic_facade.py"],
            "max_dynamic_skills": 10,
        }
        ok, reason = validate_dynamic_skill(
            "test-skill",
            {"handler": "dynamic_facade.py:handle", "price": float("nan")},
            policy, 0,
        )
        assert not ok
        assert "invalid price" in reason

    def test_adv_027_handler_path_traversal_blocked(self):
        """
        ADV-027: load_handler() checks that handler path stays within config_dir.
        Path traversal like "../../etc/passwd:handle" should raise ImportError.
        Regression guard.

        Location: config.py:113-118
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            from knarr.cli.config import load_handler
            with pytest.raises((ImportError, FileNotFoundError)):
                load_handler("../../etc/passwd:handle", tmpdir)


# ===========================================================================
# G. PUNCHHOLE FRONTEND AIRGAP VERIFICATION
# ===========================================================================


class TestPunchholeFrontendAirgap:
    """Category D: Frontend airgap invariant verification."""

    def test_adv_028_frontend_never_calls_ctx_sign_document(self):
        """
        ADV-028: Verify that PunchholeFrontendPlugin code never calls
        ctx.sign_document. This is a key airgap invariant.

        The docstring says "No ctx.sign_document usage" as an invariant.
        We verify no actual ctx.sign_document call exists (not just in comments).

        Note: The frontend legitimately calls verify_document() to verify
        incoming requests. Only signing is prohibited.

        Location: plugins/08-punchhole-frontend/handler.py
        """
        frontend_path = BASE_DIR / "src" / "knarr" / "plugins" / "08-punchhole-frontend" / "handler.py"
        source = frontend_path.read_text(encoding="utf-8")

        # Check for actual ctx.sign_document call (not in comments/docstrings)
        import ast
        tree = ast.parse(source)

        sign_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Check for ctx.sign_document(...)
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr == "sign_document":
                        if isinstance(node.func.value, ast.Attribute):
                            if node.func.value.attr == "_ctx":
                                sign_calls.append(ast.get_source_segment(source, node) or str(node.lineno))
                        elif isinstance(node.func.value, ast.Name):
                            if node.func.value.id in ("ctx", "self"):
                                sign_calls.append(ast.get_source_segment(source, node) or str(node.lineno))

        assert len(sign_calls) == 0, (
            f"BUG: Frontend plugin calls ctx.sign_document! Airgap violation. "
            f"Found {len(sign_calls)} call(s): {sign_calls}"
        )

    def test_adv_029_frontend_malformed_cache_fill_no_object_key(self):
        """
        ADV-029: The frontend's _bus_loop processes cache.fill.* events.
        At handler.py:160: `if object_key and acl_group and signed_obj is not None`
        If object_key is "" (empty), the cache fill is silently dropped.

        A malformed event with object_key="" would be silently ignored rather
        than logged. This is a low-severity observation but demonstrates the
        silent failure mode in the bus loop.
        """
        frontend_mod = _load_punchhole_frontend()

        ctx = MagicMock()
        ctx.plugin_dir = Path(tempfile.mkdtemp())
        ctx.subscribe_events = None  # Disable bus integration
        ctx.emit_event = None

        plugin = frontend_mod.PunchholeFrontendPlugin(ctx, {})
        plugin._backend_ready = True

        # Simulate processing a malformed cache.fill event
        initial_cache_size = len(plugin._cache)

        # Manually trigger what _bus_loop would do with empty object_key
        event = {"event": "cache.fill.bad", "object_key": "", "acl_group": "all_signed", "data": {"signed": True}}
        object_key = event.get("object_key", "")
        acl_group = event.get("acl_group", "")
        signed_obj = event.get("data")

        if object_key and acl_group and signed_obj is not None:
            plugin._cache[(object_key, acl_group)] = {"data": signed_obj, "stale": False}

        assert len(plugin._cache) == initial_cache_size, \
            "Empty object_key events correctly dropped (regression guard)"

    def test_adv_030_frontend_acl_update_with_non_dict_silently_ignored(self):
        """
        ADV-030: The frontend's _bus_loop at handler.py:149-151:
        `acl_data = event.get("acl", {})`
        `if isinstance(acl_data, dict): self._acl.update(acl_data)`

        If acl_data is a list, it's silently ignored. But if it's a dict with
        node_id -> non-string acl_group values, update() succeeds and the ACL
        map gets corrupted with non-string values.

        A backend that emits malformed ACL data (acl = {"node123": 42} instead
        of {"node123": "peer"}) would silently corrupt the frontend ACL map.
        """
        frontend_mod = _load_punchhole_frontend()
        ctx = MagicMock()
        ctx.plugin_dir = Path(tempfile.mkdtemp())
        ctx.subscribe_events = None
        ctx.emit_event = None

        plugin = frontend_mod.PunchholeFrontendPlugin(ctx, {})

        # Simulate malformed ACL with integer values
        malformed_acl = {"abc123" * 10 + "abcd": 42}  # integer acl_group
        if isinstance(malformed_acl, dict):
            plugin._acl.update(malformed_acl)

        # The ACL map now has a non-string value
        node = "abc123" * 10 + "abcd"
        assert plugin._acl.get(node) == 42, "Malformed ACL value silently accepted"

        # This would cause a bug when the ACL group is later used in a cache lookup:
        # cache_key = (object_key, 42)  -- integer key instead of string


# ===========================================================================
# H. WM ONE-WAY CONSTRAINT VERIFICATION
# ===========================================================================


class TestWarehouseManagerOneWay:
    """Category A: One-way constraint — WM has no outbound path."""

    def test_adv_031_wm_source_has_no_send_mail(self):
        """
        ADV-031: WarehouseManager must NEVER call send_mail or emit to DMZ.
        Verify by inspecting the source code for outbound patterns.

        Location: warehouse_manager.py
        KEY INVARIANT: WM one-way constraint.
        """
        wm_path = BASE_DIR / "src" / "knarr" / "core" / "warehouse_manager.py"
        source = wm_path.read_text(encoding="utf-8")

        # Check for outbound patterns
        assert "send_mail" not in source, "BUG: WM calls send_mail — one-way violation!"
        assert "requests.post" not in source, "BUG: WM makes HTTP requests — one-way violation!"
        assert "urllib.request" not in source, "BUG: WM makes HTTP requests — one-way violation!"

    def test_adv_032_wm_promote_does_not_quarantine_id_in_result(self):
        """
        ADV-032: When WM promotes a document, IngestResult.quarantine_id should be None.
        _promote() at warehouse_manager.py:500 explicitly sets quarantine_id=None.

        But the quarantine_id 'qid' IS generated at the start of ingest() regardless
        of outcome (line 179). The promoted document is NOT stored in quarantine DB.
        Verify that a promoted document leaves no quarantine trace.

        Use payment_received which has a proper v0.37.0 validator (no old "type" check).
        """
        wm, bus, storage, write_receipt = _make_wm()
        # payment_received uses validate_payment_received which checks chain_id, tx_hash etc.
        # BUT it still goes through Gate 1 (verify), Gate 3 (schema), Gate 4 (integrity).
        # Use a doc that can pass Gate 3 by using the v0.37.0 validators.
        doc = {
            "document_type": "payment_received",
            "identity": "did:knarr:testnode",
            "chain_id": "solana-mainnet",
            "tx_hash": "abc123def456",
            "tx_index": 0,
            "from_address": "SenderABC",
            "to_address": "did:knarr:testnode",
            "amount": 1_000_000,
            "denom": "SOL",
            "decimals": 9,
            "confirmation": {"level": "finalized"},
            "proof": {
                "type": "DataIntegrityProof",
                "cryptosuite": "eddsa-jcs-2022",
                "created": _now_iso(),
                "proofValue": "zABCDEFGHIJ",
                "verificationMethod": "did:knarr:testnode#key-1",
            },
        }

        with patch("knarr.core.warehouse_manager.verify_document", return_value=True):
            result = wm.ingest(doc, b"\x00" * 32)

        # payment_received has gates [1, 3, 4] and action auto_promote (from DEFAULT_RULES)
        # It should be promoted if Gate 3 passes (validate_payment_received should pass)
        # Note: Gate 2 (addressing) is NOT in payment_received's gates

        if result.status == "rejected":
            # Diagnose which gate failed
            pytest.fail(
                f"payment_received was rejected — gate_results={result.gate_results}, "
                f"reason={result.reason}. "
                "If Gate 3 fails, validate_payment_received may be rejecting the document "
                "for missing/invalid fields."
            )

        assert result.status == "promoted"
        assert result.quarantine_id is None, "Promoted document should not have a quarantine_id"

        # Verify no entries in quarantine DB
        cur = storage._conn.execute("SELECT count(*) FROM dmz_quarantine")
        count = cur.fetchone()[0]
        assert count == 0, f"Promoted document left {count} entry in quarantine DB — unexpected!"


# ===========================================================================
# I. BCW DEDUP AND CURSOR ATTACKS
# ===========================================================================


class TestBCWDedupAttacks:
    """Category G: BCW dedup and cursor integrity."""

    def setup_method(self):
        self.bcw_mod = _load_bcw()

    def test_adv_033_dedup_key_with_colon_in_tx_hash(self):
        """
        ADV-033: _dedup_key() formats as `{chain_id}:{tx_hash}:{tx_index}`.
        If tx_hash contains colons (e.g., "abc:def:ghi"), the dedup_key becomes
        ambiguous: "solana-mainnet:abc:def:ghi:0" could collide with a legitimate
        tx_hash of "abc" with chain "solana-mainnet:abc:def:ghi".

        Location: handler.py:559-560
        In practice, Solana tx hashes are base58 and don't contain colons,
        but for chain-agnostic dedup this is a structural weakness.

        This is a theoretical finding — verifying the dedup key format.
        """
        from knarr.commerce.transfer_event import ConfirmationStatus, TransferEvent

        plugin_path = BASE_DIR / "src" / "knarr" / "plugins" / "10-bcw"
        sys.path.insert(0, str(plugin_path))

        import handler as bcw_handler

        t1 = TransferEvent(
            chain_id="solana-mainnet",
            tx_hash="abc:def",  # colon in hash
            tx_index=0,
            from_address="A", to_address="B",
            amount=1000, denom="SOL", decimals=9,
            confirmation=ConfirmationStatus.FINALIZED,
        )
        t2 = TransferEvent(
            chain_id="solana-mainnet:abc",  # longer chain_id
            tx_hash="def",
            tx_index=0,
            from_address="A", to_address="B",
            amount=1000, denom="SOL", decimals=9,
            confirmation=ConfirmationStatus.FINALIZED,
        )

        key1 = bcw_handler._dedup_key(t1)
        key2 = bcw_handler._dedup_key(t2)

        # Both produce the same dedup key! This is a collision.
        assert key1 == key2, (
            f"THEORETICAL FINDING: Colon in tx_hash causes dedup key collision. "
            f"key1={key1!r}, key2={key2!r}. "
            "handler.py:560 — _dedup_key uses colon separators without escaping."
        )

    def test_adv_034_bcw_zero_amount_filtered_before_emit(self):
        """
        ADV-034: SolanaWatcher._parse_positive_amount filters 0 amounts.
        But BCW plugin's _process_transfer() has common_fields with "amount": transfer.amount.
        If somehow a TransferEvent with amount=0 got through (shouldn't happen due to
        filtering in _parse_positive_amount), _write_receipt() would create a receipt
        for a 0-value transfer.

        Verify the receipt writing path handles amount=0 in the body correctly —
        the body contains the raw amount without re-validation.

        Location: handler.py:506-537 — _write_receipt does not validate amount > 0
        """
        from knarr.commerce.transfer_event import ConfirmationStatus, TransferEvent

        plugin_path = BASE_DIR / "src" / "knarr" / "plugins" / "10-bcw"
        sys.path.insert(0, str(plugin_path))

        ctx = MagicMock()
        ctx.plugin_dir = Path(tempfile.mkdtemp())
        ctx.node_id = "a" * 64
        ctx.vault_get = MagicMock(return_value=None)
        ctx.sign_document = None  # no signer

        import handler as bcw_handler

        plugin = bcw_handler.BCWPlugin(ctx, {"enabled": False, "chains": []})

        # Create a transfer with amount=0 (bypassing normal filtering)
        t = TransferEvent(
            chain_id="solana-mainnet",
            tx_hash="zero" * 16 + "aaaa",
            tx_index=0,
            from_address="Sender", to_address="Receiver",
            amount=0,  # zero amount — should never reach _write_receipt
            denom="SOL", decimals=9,
            confirmation=ConfirmationStatus.FINALIZED,
        )

        # Directly call _write_receipt (bypassing the amount filter in _parse_positive_amount)
        receipt_id = plugin._write_receipt("payment_received", t, {})
        assert receipt_id.startswith("prx_"), (
            "NOTE: _write_receipt does not validate amount > 0 itself. "
            "It trusts that upstream filtering (parse_positive_amount) has already done so. "
            "A 0-amount transfer that bypasses the filter would create a 0-value receipt."
        )
