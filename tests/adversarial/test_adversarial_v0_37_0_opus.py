"""Adversarial tests for v0.37.0 Vordur — Opus 4.6 attack model.

Each test targets ONE finding. Tests that FAIL prove bugs exist.
Tests that PASS are regression guards.

Mock I/O aggressively — no real network, no real crypto.
"""

import asyncio
import importlib.util
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — src MUST be first for import resolution
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent.parent
PROPOSED_SRC = str(BASE_DIR / "src")
ROOT_SRC = str(BASE_DIR.parent / "src")

# Force src first on sys.path
for p in [ROOT_SRC, PROPOSED_SRC]:
    if p in sys.path:
        sys.path.remove(p)
sys.path.insert(0, PROPOSED_SRC)
sys.path.insert(1, ROOT_SRC)

# Purge any cached knarr modules so they reload from the correct path
_to_purge = [k for k in sys.modules if k.startswith("knarr")]
for k in _to_purge:
    del sys.modules[k]

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

from knarr.commerce.documents import Document, _PREFIX_MAP, _TYPE_REGISTRY
from knarr.commerce.schemas import (
    validate_cache_object,
    validate_configuration_order,
    validate_payment_finalized,
    validate_payment_received,
    validate_punchhole_card,
    validate_wallet_transfer,
)

# ---------------------------------------------------------------------------
# Helpers — WM
# ---------------------------------------------------------------------------

NODE_ID = "a" * 64
IDENTITY_FRAGMENTS = [
    NODE_ID,
    f"did:knarr:{NODE_ID}",
    f"did:knarr:{NODE_ID}#key-1",
    f"did:knarr:{NODE_ID}#cockpit-1",
    f"did:knarr:{NODE_ID}#thrall-1",
]
FAKE_PUBKEY = b"\x01" * 32


class _QuarantineStorage:
    """In-memory SQLite with dmz_quarantine table."""

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

    def quarantine_store(self, id, document_type, document_json,
                         originator_pubkey, status, gate_results, reason):
        now = time.time()
        self._conn.execute(
            """INSERT OR REPLACE INTO dmz_quarantine
               (id, document_type, document_json, originator_pubkey,
                status, gate_results, reason, received_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, document_type, document_json, originator_pubkey,
             status, gate_results, reason, now),
        )
        self._conn.commit()

    def quarantine_get(self, id):
        cursor = self._conn.execute(
            """SELECT id, document_type, document_json, originator_pubkey,
                      status, gate_results, reason, received_at,
                      promoted_at, resolved_at
               FROM dmz_quarantine WHERE id = ?""", (id,))
        row = cursor.fetchone()
        if not row:
            return None
        return dict(zip(
            ["id", "document_type", "document_json", "originator_pubkey",
             "status", "gate_results", "reason", "received_at",
             "promoted_at", "resolved_at"], row))

    def quarantine_update_status(self, id, status, reason=None,
                                 promoted_at=None, resolved_at=None):
        self._conn.execute(
            """UPDATE dmz_quarantine
               SET status = ?, reason = COALESCE(?, reason),
                   promoted_at = COALESCE(?, promoted_at),
                   resolved_at = COALESCE(?, resolved_at)
               WHERE id = ?""",
            (status, reason, promoted_at, resolved_at, id))
        self._conn.commit()


def _make_signed_doc(doc_type="credit_note", identity=None,
                     counterparty=None, vm=None):
    """Build a minimal signed document for testing."""
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
            "proofValue": "z" + "A" * 86,
        },
    }
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
            "original_receipt_id": "prx_123",
            "finality": {"level": "finalized"},
        },
        "configuration_order": {
            "target": "exposure_schema", "operation": "upsert_object",
            "changes": {"object_key": "economy.summary"},
        },
        "cache_object": {
            "object_key": "economy.summary",
            "data": {"balance": 100}, "granularity": {"balance": "exact"},
        },
        "punchhole_card": {
            "for_node": "abc", "for_access_level": "peer",
            "available": [], "not_available": [],
        },
        "wallet_transfer": {
            "chain_id": "solana-mainnet", "tx_hash": "abc",
            "from_address": "S", "to_address": "R",
            "amount": 100, "denom": "SOL", "decimals": 9,
            "transfer_type": "master_to_derived",
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
        bus=bus,
        storage=st,
        config=config,
        write_receipt_cb=write_receipt_cb,
    )
    return wm, bus, st, write_receipt_cb


# ---------------------------------------------------------------------------
# Helpers — Punchhole backend (granularity)
# ---------------------------------------------------------------------------

def _load_backend_module():
    base = Path(__file__).resolve().parent.parent.parent
    plugin_path = base / "src" / "knarr" / "plugins" / "09-punchhole-backend"
    sys.path.insert(0, str(plugin_path))
    spec = importlib.util.spec_from_file_location(
        "ph_backend_adv", str(plugin_path / "handler.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.path.remove(str(plugin_path))
    return mod


_backend_mod = _load_backend_module()
_apply_granularity = _backend_mod._apply_granularity
_build_data_dict = _backend_mod._build_data_dict


# ---------------------------------------------------------------------------
# Helpers — BCW (dynamic plugin load)
# ---------------------------------------------------------------------------

def _load_bcw_modules():
    base = Path(__file__).resolve().parent.parent.parent
    plugin_path = base / "src" / "knarr" / "plugins" / "10-bcw"
    sys.path.insert(0, str(plugin_path))

    import knarr
    knarr.__path__.insert(0, str(base / "src" / "knarr"))
    import knarr.commerce
    knarr.commerce.__path__.insert(0, str(base / "src" / "knarr" / "commerce"))

    import handler as bcw_handler
    import solana as bcw_solana
    sys.path.remove(str(plugin_path))
    return bcw_handler, bcw_solana


_bcw_handler, _bcw_solana = _load_bcw_modules()
from knarr.commerce.transfer_event import ConfirmationStatus, TransferEvent


# ===========================================================================
# ADV-001: Gate 5 authorization bypass — #cockpit-1 substring match
# ===========================================================================

class TestAdv001CockpitSubstringBypass:
    """Gate 5 checks `'#cockpit-1' in vm`. The substring match means
    a verificationMethod like '#cockpit-1-fake' or '#not-cockpit-1' passes.

    Location: src/knarr/core/warehouse_manager.py:402
    Severity: HIGH
    """

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_adv_001_cockpit_substring_bypass(self, mock_verify):
        """A configuration_order signed by '#cockpit-1-fake' should FAIL
        gate 5, but the substring check passes it through."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc(
            "configuration_order",
            vm=f"did:knarr:{'b' * 64}#cockpit-1-fake",
        )
        result = wm.ingest(doc, FAKE_PUBKEY)
        # BUG: This passes gate 5 because '#cockpit-1' in '#cockpit-1-fake'
        # Expected: gate 5 fail (unauthorized fragment)
        # Actual: gate 5 pass (substring match allows it)
        assert result.gate_results.get(5) == "fail", \
            "Gate 5 should reject '#cockpit-1-fake' — substring match bypass"


# ===========================================================================
# ADV-002: Gate 2 addressing — empty identity_fragments passes trivially
# ===========================================================================

class TestAdv002EmptyIdentityFragments:
    """If WM is initialized with empty identity_fragments, gate 2 becomes
    a blocking gate that rejects everything. But what if the document has
    an empty string in a relevant field?

    Location: src/knarr/core/warehouse_manager.py:337-349
    Severity: MEDIUM
    """

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_adv_002_empty_string_identity_match(self, mock_verify):
        """A document with identity='' should NOT pass gate 2, even if
        WM was constructed with '' in identity_fragments."""
        from knarr.core.warehouse_manager import WarehouseManager

        bus = MagicMock()
        st = _QuarantineStorage()
        # BUG CANDIDATE: empty string in identity_fragments
        wm = WarehouseManager(
            node_id=NODE_ID,
            identity_fragments=[""],  # empty string
            bus=bus,
            storage=st,
            config={"debug": True},
            write_receipt_cb=MagicMock(),
        )
        doc = _make_signed_doc("credit_note", identity="")
        result = wm.ingest(doc, FAKE_PUBKEY)
        # Empty string matches empty string — gate 2 passes a doc addressed to nobody
        assert result.gate_results.get(2) == "fail", \
            "Gate 2 should not match empty string identity"


# ===========================================================================
# ADV-003: Gate 2 addressing — verificationMethod substring match
# ===========================================================================

class TestAdv003VMSubstringMatch:
    """Gate 2 checks if any identity_fragment is a substring of
    proof.verificationMethod. This means a foreign node whose DID
    happens to CONTAIN our node_id as a substring could pass gate 2.

    Location: src/knarr/core/warehouse_manager.py:346-348
    Severity: HIGH
    """

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_adv_003_vm_substring_false_positive(self, mock_verify):
        """A document with VM containing our node_id as a prefix/substring
        should not pass gate 2 if it's actually a different DID."""
        wm, bus, st, wr = _make_wm()
        # Foreign document where VM happens to contain our node_id
        # but it's embedded in a longer string
        foreign_vm = f"did:knarr:{NODE_ID}extra_suffix#key-1"
        doc = _make_signed_doc(
            "credit_note",
            identity="c" * 64,
            counterparty="d" * 64,
            vm=foreign_vm,
        )
        result = wm.ingest(doc, FAKE_PUBKEY)
        # BUG: Gate 2 passes because `NODE_ID in foreign_vm` is True
        # The check is `frag in vm` which is a substring check
        assert result.gate_results.get(2) == "fail", \
            "Gate 2 should not pass on VM substring match"


# ===========================================================================
# ADV-004: Gate 3 — validator exception fails OPEN
# ===========================================================================

class TestAdv004ValidatorExceptionFailOpen:
    """If a schema validator raises an unexpected exception, gate 3
    should fail CLOSED (reject). Let's verify.

    Location: src/knarr/core/warehouse_manager.py:208-226
    Severity: CRITICAL (if fails open)
    """

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_adv_004_validator_exception_fails_closed(self, mock_verify):
        """Validator that raises should result in gate 3 failure."""
        wm, bus, st, wr = _make_wm()

        # Inject a crashing validator
        def _crashing_validator(body):
            raise RuntimeError("validator crash!")

        validators = wm._get_validators()
        validators["payment_received"] = _crashing_validator

        doc = _make_signed_doc("payment_received")
        result = wm.ingest(doc, FAKE_PUBKEY)
        # If the exception propagates uncaught, this test itself will error.
        # If it fails open (passes gate 3), status would be "promoted".
        # Expected: rejected/quarantined
        assert result.status == "rejected", \
            "Gate 3 validator exception should fail CLOSED (reject)"
        assert result.gate_results.get(3) == "fail"


# ===========================================================================
# ADV-005: Gate 3 — document body is None
# ===========================================================================

class TestAdv005BodyIsNone:
    """Gate 3 extracts body = document.get('body', document). If body is
    explicitly set to None, the validator receives None instead of a dict.

    Location: src/knarr/core/warehouse_manager.py:213
    Severity: MEDIUM
    """

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_adv_005_body_none_handled(self, mock_verify):
        """Document with body=None should not crash gate 3."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("payment_received")
        doc["body"] = None
        result = wm.ingest(doc, FAKE_PUBKEY)
        # body=None means validator gets None, which should fail schema validation
        # but should NOT crash the gate
        assert result.status == "rejected", \
            "Document with body=None should be rejected, not crash"


# ===========================================================================
# ADV-006: Gate 5 — vm is None
# ===========================================================================

class TestAdv006VMIsNone:
    """If proof.verificationMethod is None (not a string), the '#cockpit-1' in vm
    check in gate 5 would raise TypeError since `in` on None is invalid.

    Location: src/knarr/core/warehouse_manager.py:398-402
    Severity: MEDIUM
    """

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_adv_006_vm_none_no_crash(self, mock_verify):
        """configuration_order with vm=None should not crash gate 5."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("configuration_order")
        doc["proof"]["verificationMethod"] = None
        result = wm.ingest(doc, FAKE_PUBKEY)
        # Should reject cleanly, not crash with TypeError
        assert result.status == "rejected"
        assert result.gate_results.get(5) == "fail"


# ===========================================================================
# ADV-007: Approve/reject race — approve after reject
# ===========================================================================

class TestAdv007ApproveAfterReject:
    """If reject() sets status to 'rejected', approve() should not
    be able to re-promote it. The approve check is:
    `status not in ('pending', 'approved')` — but actually it checks for
    those two, so rejected items SHOULD fail. Let's verify.

    Location: src/knarr/core/warehouse_manager.py:279
    Severity: HIGH (if race condition exists)
    """

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_adv_007_approve_after_reject(self, mock_verify):
        """Cannot approve a rejected item."""
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
        assert result.status == "held"

        # Reject first
        wm.reject(result.quarantine_id, "operator says no")
        # Now try to approve
        ok = wm.approve(result.quarantine_id)
        assert not ok, "Should not be able to approve a rejected item"


# ===========================================================================
# ADV-008: Granularity range:N with NaN range value
# ===========================================================================

class TestAdv008RangeNaNParameter:
    """range:NaN should not crash. The code parses n = float(n_str),
    which would give NaN. Then n <= 0 is False for NaN, so it proceeds
    to math.floor(value / NaN) * NaN which gives NaN.

    Location: src/knarr/plugins/09-punchhole-backend/handler.py:104-118
    Severity: MEDIUM
    """

    def test_adv_008_range_nan_value(self):
        """range:NaN should not produce NaN output — should return None or value."""
        result = _apply_granularity(100.0, "range:NaN")
        # NaN parses as float. n <= 0 is False for NaN, so it enters the
        # division path. floor(100.0 / NaN) * NaN = NaN.
        # BUG: Returns NaN instead of None or the original value
        assert result is None or (isinstance(result, (int, float)) and math.isfinite(result)), \
            f"range:NaN should not produce NaN, got {result}"


# ===========================================================================
# ADV-009: Granularity range:Inf
# ===========================================================================

class TestAdv009RangeInfParameter:
    """range:Inf — float('Inf') > 0 is True, so n > 0 passes.
    floor(100 / Inf) * Inf = 0 * Inf = NaN.

    Location: src/knarr/plugins/09-punchhole-backend/handler.py:104-118
    Severity: MEDIUM
    """

    def test_adv_009_range_inf_value(self):
        """range:Inf should not produce NaN."""
        result = _apply_granularity(100.0, "range:Inf")
        # floor(100.0 / Inf) = 0.0, then 0.0 * Inf = nan
        assert result is None or (isinstance(result, (int, float)) and math.isfinite(result)), \
            f"range:Inf should not produce NaN, got {result}"


# ===========================================================================
# ADV-010: Granularity range:N with int value and fractional N
# ===========================================================================

class TestAdv010RangeIntDivisionFractional:
    """When value is int and N is fractional (e.g., range:0.5),
    int(n) = 0, causing ZeroDivisionError in `value // int(n)`.

    Location: src/knarr/plugins/09-punchhole-backend/handler.py:115
    Severity: HIGH
    """

    def test_adv_010_range_int_value_fractional_n(self):
        """range:0.5 on an int value should not crash with ZeroDivisionError."""
        # int(0.5) = 0, then value // 0 raises ZeroDivisionError
        try:
            result = _apply_granularity(100, "range:0.5")
        except ZeroDivisionError:
            pytest.fail("range:0.5 on int value raised ZeroDivisionError")
        # Should return something finite
        assert result is not None


# ===========================================================================
# ADV-011: Granularity recent:-1
# ===========================================================================

class TestAdv011RecentNegative:
    """recent:-1 — n = -1, n > 0 is False, so returns [].
    This is correct behavior. But recent:0 also returns [].

    Location: src/knarr/plugins/09-punchhole-backend/handler.py:95-101
    Severity: LOW (regression guard)
    """

    def test_adv_011_recent_zero(self):
        """recent:0 should return empty list, not the full list."""
        result = _apply_granularity([1, 2, 3, 4, 5], "recent:0")
        assert result == [], f"recent:0 should return [], got {result}"

    def test_adv_011_recent_negative(self):
        """recent:-1 should return empty list."""
        result = _apply_granularity([1, 2, 3, 4, 5], "recent:-1")
        assert result == [], f"recent:-1 should return [], got {result}"


# ===========================================================================
# ADV-012: Schema validator — payment_received with negative amount
# ===========================================================================

class TestAdv012NegativeAmount:
    """_validate_chain_tx checks amt > 0, so negative should fail.
    Regression guard.

    Location: src/knarr/commerce/schemas.py:113
    Severity: LOW (regression guard)
    """

    def test_adv_012_negative_amount(self):
        ok, err = validate_payment_received({
            "chain_id": "solana-mainnet", "tx_hash": "abc", "tx_index": 0,
            "from_address": "S", "to_address": "R",
            "amount": -1, "denom": "KNARR", "decimals": 9,
            "confirmation": {"level": "finalized"},
        })
        assert not ok, "Negative amount should fail validation"

    def test_adv_012_inf_amount(self):
        ok, err = validate_payment_received({
            "chain_id": "solana-mainnet", "tx_hash": "abc", "tx_index": 0,
            "from_address": "S", "to_address": "R",
            "amount": float("inf"), "denom": "KNARR", "decimals": 9,
            "confirmation": {"level": "finalized"},
        })
        assert not ok, "Inf amount should fail validation"

    def test_adv_012_nan_amount(self):
        ok, err = validate_payment_received({
            "chain_id": "solana-mainnet", "tx_hash": "abc", "tx_index": 0,
            "from_address": "S", "to_address": "R",
            "amount": float("nan"), "denom": "KNARR", "decimals": 9,
            "confirmation": {"level": "finalized"},
        })
        assert not ok, "NaN amount should fail validation"


# ===========================================================================
# ADV-013: Schema validator — configuration_order with SQL injection operation
# ===========================================================================

class TestAdv013ConfigOrderSQLInjection:
    """configuration_order validator checks operation in {'upsert_object',
    'modify_access', 'remove_object'}. 'DROP TABLE' should fail.

    Location: src/knarr/commerce/schemas.py:179-180
    Severity: LOW (regression guard)
    """

    def test_adv_013_sql_injection_operation(self):
        ok, err = validate_configuration_order({
            "target": "exposure_schema",
            "operation": "DROP TABLE dmz_quarantine; --",
            "changes": {},
        })
        assert not ok


# ===========================================================================
# ADV-014: Document type prefix uniqueness
# ===========================================================================

class TestAdv014PrefixUniqueness:
    """All 8 new document types must have unique prefixes in _PREFIX_MAP.

    Location: src/knarr/commerce/documents.py:86-110
    Severity: HIGH (if collision exists)
    """

    def test_adv_014_prefix_uniqueness(self):
        """All prefix values must be unique."""
        prefixes = list(_PREFIX_MAP.values())
        assert len(prefixes) == len(set(prefixes)), \
            f"Duplicate prefixes found: {[p for p in prefixes if prefixes.count(p) > 1]}"

    def test_adv_014_all_v37_types_registered(self):
        """All 8 new v0.37.0 types have entries in both registries."""
        new_types = [
            "payment_received", "payment_finalized", "payment_executed",
            "wallet_transfer", "wallet_withdrawal",
            "configuration_order", "punchhole_card", "cache_object",
        ]
        for t in new_types:
            assert t in _TYPE_REGISTRY, f"{t} missing from _TYPE_REGISTRY"
            assert t in _PREFIX_MAP, f"{t} missing from _PREFIX_MAP"


# ===========================================================================
# ADV-015: BCW — address derivation with empty node_id
# ===========================================================================

class TestAdv015BCWEmptyNodeID:
    """derive_counterparty_address validates len(node_id) == 64.
    Empty string should raise ValueError.

    Location: src/knarr/plugins/10-bcw/handler.py:33
    Severity: LOW (regression guard)
    """

    def test_adv_015_empty_node_id(self):
        with pytest.raises(ValueError, match="64 hex chars"):
            _bcw_handler.derive_counterparty_address(
                b"\x01" * 32, "", "solana-mainnet")

    def test_adv_015_non_hex_node_id(self):
        """Non-hex 64-char node_id should fail in sha256 computation,
        but the code does node_id.encode('utf-8') not bytes.fromhex,
        so it silently accepts any 64-char string as a seed."""
        # This is a DESIGN observation: the function only checks length,
        # not that node_id is valid hex
        non_hex = "x" * 64
        # This should either raise or produce a valid (but wrong) address
        try:
            addr = _bcw_handler.derive_counterparty_address(
                b"\x01" * 32, non_hex, "solana-mainnet")
            # BUG: Any 64-char string is accepted, not just hex
            # This is a finding but not necessarily exploitable
            assert addr is not None
        except ValueError:
            pass  # Would be the correct behavior


# ===========================================================================
# ADV-016: BCW — self-transfer classification edge case (from == to)
# ===========================================================================

class TestAdv016SelfTransferClassification:
    """When from_address == to_address AND both are self-owned,
    _classify_transfer returns 'wallet_transfer'. What about when
    from_address == to_address but the address is NOT self-owned?

    Location: src/knarr/plugins/10-bcw/handler.py:480-496
    Severity: LOW
    """

    def test_adv_016_self_transfer_same_address(self):
        """Transfer where from == to and both self-owned -> wallet_transfer."""
        seed = b"\x01" * 32
        ctx = MagicMock()
        ctx.plugin_dir = Path(tempfile.mkdtemp())
        ctx.node_id = "a" * 64
        ctx.subscribe_events.return_value = MagicMock(
            poll=MagicMock(return_value=[]))
        ctx.vault_get.side_effect = lambda *args: "01" * 32 if args[-1] == "bcw_master_seed" else None
        ctx.get_peers.return_value = []
        ctx.emit_event = MagicMock()
        ctx.log = MagicMock()
        ctx.sign_document.side_effect = lambda doc: {**doc, "proof": {"type": "test"}}

        plugin = _bcw_handler.BCWPlugin(ctx, {
            "enabled": True,
            "chains": [{"chain_id": "solana-mainnet", "rpc_url": "http://mock"}],
        })

        self_addr = list(plugin._self_owned_addresses)[0] if plugin._self_owned_addresses else "addr1"
        event = TransferEvent(
            chain_id="solana-mainnet", tx_hash="tx-self",
            tx_index=0, from_address=self_addr, to_address=self_addr,
            amount=100_000, denom="SOL", decimals=9,
            confirmation=ConfirmationStatus.FINALIZED,
        )
        result = plugin._classify_transfer(event)
        # from_self=True, to_self=True -> wallet_transfer
        assert result == "wallet_transfer"


# ===========================================================================
# ADV-017: Dynamic skills — TOML injection via skill name
# ===========================================================================

class TestAdv017TOMLInjectionSkillName:
    """write_dynamic_skill writes section headers like [skills.{name}].
    A skill name containing ']' or newline could inject TOML.

    Location: src/knarr/cli/config.py:354
    Severity: HIGH
    """

    def test_adv_017_skill_name_with_bracket(self):
        """Skill name with ] should be rejected by validate_dynamic_skill."""
        from knarr.cli.config import validate_dynamic_skill
        policy = {
            "dynamic_enabled": True,
            "dynamic_allowed_handlers": ["dynamic_facade.py"],
            "dynamic_price_floor": 0.5,
            "dynamic_price_ceiling": 50.0,
        }
        ok, reason = validate_dynamic_skill(
            "test]\n[evil.section",
            {"handler": "dynamic_facade.py:handle", "price": 1.0},
            policy, 0,
        )
        assert not ok, "Skill name with ] should be rejected"

    def test_adv_017_skill_name_with_newline(self):
        """Skill name with newline should be rejected."""
        from knarr.cli.config import validate_dynamic_skill
        policy = {
            "dynamic_enabled": True,
            "dynamic_allowed_handlers": ["dynamic_facade.py"],
        }
        ok, reason = validate_dynamic_skill(
            "test\nevil",
            {"handler": "dynamic_facade.py:handle", "price": 1.0},
            policy, 0,
        )
        assert not ok, "Skill name with newline should be rejected"


# ===========================================================================
# ADV-018: Dynamic skills — handler path traversal
# ===========================================================================

class TestAdv018HandlerPathTraversal:
    """validate_dynamic_skill checks handler basename against allowed list.
    But what about the value written to TOML? If handler contains
    '../../etc/passwd:handle', the basename is 'passwd' not in allowed.
    But the write_dynamic_skill function doesn't validate.

    Location: src/knarr/cli/config.py:313-322
    Severity: MEDIUM (validate catches it, but write doesn't)
    """

    def test_adv_018_path_traversal_basename_check(self):
        """Path traversal handler should be rejected by validate."""
        from knarr.cli.config import validate_dynamic_skill
        policy = {
            "dynamic_enabled": True,
            "dynamic_allowed_handlers": ["dynamic_facade.py"],
        }
        ok, reason = validate_dynamic_skill(
            "test-skill",
            {"handler": "../../etc/passwd:handle", "price": 1.0},
            policy, 0,
        )
        assert not ok, "Path traversal handler should be rejected"

    def test_adv_018_write_without_validate(self):
        """write_dynamic_skill does NOT validate — it writes anything."""
        from knarr.cli.config import write_dynamic_skill, load_dynamic_skills
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            # Write a skill with a traversal handler WITHOUT validating first
            ok = write_dynamic_skill(td_path, "evil-skill", {
                "handler": "../../etc/passwd:handle",
                "price": 999.0,
            })
            assert ok, "write_dynamic_skill should succeed (no validation)"
            # The handler is now in the file — load_handler would be needed
            # to actually execute it, and load_handler has its own path check
            skills = load_dynamic_skills(td_path)
            assert "evil-skill" in skills


# ===========================================================================
# ADV-019: Dynamic skills — TOML value injection via handler string
# ===========================================================================

class TestAdv019TOMLValueInjection:
    """write_dynamic_skill formats string values as '{k} = "{v}"'.
    A handler value containing a quote + newline could inject TOML.

    Location: src/knarr/cli/config.py:359
    Severity: HIGH
    """

    def test_adv_019_handler_with_embedded_quote(self):
        """Handler value with embedded double-quote should not break TOML."""
        from knarr.cli.config import write_dynamic_skill, load_dynamic_skills
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            # Handler with embedded quote that could break the TOML format
            malicious_handler = 'legit.py"\nevil_key = "evil_value'
            ok = write_dynamic_skill(td_path, "test-skill", {
                "handler": malicious_handler,
                "price": 1.0,
            })
            # The write succeeds but the resulting TOML is malformed
            if ok:
                skills = load_dynamic_skills(td_path)
                if "test-skill" in skills:
                    # If the load succeeded, check if the handler was corrupted
                    loaded_handler = skills["test-skill"].get("handler", "")
                    # BUG: The handler value is mangled by the quote injection
                    assert loaded_handler == malicious_handler, \
                        f"TOML injection: handler mangled to {loaded_handler!r}"


# ===========================================================================
# ADV-020: Granularity _build_data_dict — hidden returns None, kept in dict
# ===========================================================================

class TestAdv020HiddenReturnsNoneInDict:
    """_build_data_dict has a redundant check:
        if val is None and control == 'hidden': continue
    But `control == 'hidden'` is already caught earlier (continue).
    The issue is: if a non-hidden control returns None (e.g., NaN range),
    it gets INCLUDED in the output dict as field=None.

    Location: src/knarr/plugins/09-punchhole-backend/handler.py:124-138
    Severity: MEDIUM
    """

    def test_adv_020_nan_value_in_data_dict(self):
        """NaN value with range control produces None in output dict."""
        raw = {"balance": float("nan"), "name": "test"}
        fields = ["balance", "name"]
        granularity = {"balance": "range:50", "name": "exact"}
        result = _build_data_dict(raw, fields, granularity)
        # balance should be excluded (None from NaN guard), not included as None
        if "balance" in result:
            assert result["balance"] is not None, \
                "NaN value should not leak as None into disclosure output"


# ===========================================================================
# ADV-021: WM pubkey length — 31-byte or 33-byte pubkey
# ===========================================================================

class TestAdv021PubkeyLength:
    """Gate 1 creates VerifyKey(originator_pubkey). PyNaCl VerifyKey expects
    exactly 32 bytes. 31 or 33 bytes should raise an exception.
    The exception handler catches this, so gate 1 fails. Regression guard.

    Location: src/knarr/core/warehouse_manager.py:184
    Severity: LOW (regression guard)
    """

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_adv_021_short_pubkey(self, mock_verify):
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("credit_note")
        result = wm.ingest(doc, b"\x01" * 31)
        assert result.status == "rejected"
        assert result.gate_results.get(1) == "fail"

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_adv_021_long_pubkey(self, mock_verify):
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("credit_note")
        result = wm.ingest(doc, b"\x01" * 33)
        assert result.status == "rejected"
        assert result.gate_results.get(1) == "fail"


# ===========================================================================
# ADV-022: Gate 4 — proof.created as epoch float
# ===========================================================================

class TestAdv022ProofCreatedEpochFloat:
    """Gate 4 checks isinstance(created, str). If created is a float (epoch),
    it should fail. Regression guard.

    Location: src/knarr/core/warehouse_manager.py:364-366
    Severity: LOW (regression guard)
    """

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_adv_022_epoch_float_rejected(self, mock_verify):
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("payment_received")
        doc["proof"]["created"] = time.time()  # float, not string
        result = wm.ingest(doc, FAKE_PUBKEY)
        assert result.status == "rejected"
        assert result.gate_results.get(4) == "fail"


# ===========================================================================
# ADV-023: Gate 4 — proofValue without 'z' prefix
# ===========================================================================

class TestAdv023ProofValueNoPrefix:
    """Gate 4 checks proofValue starts with 'z'. Non-multibase values
    should fail. Regression guard.

    Location: src/knarr/core/warehouse_manager.py:389-391
    Severity: LOW (regression guard)
    """

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_adv_023_no_z_prefix(self, mock_verify):
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc("payment_received")
        doc["proof"]["proofValue"] = "AAAAAA"  # no 'z' prefix
        result = wm.ingest(doc, FAKE_PUBKEY)
        assert result.status == "rejected"
        assert result.gate_results.get(4) == "fail"


# ===========================================================================
# ADV-024: Schema — punchhole_card with available = "string"
# ===========================================================================

class TestAdv024PunchholeCardAvailableString:
    """validate_punchhole_card checks isinstance(available, list).
    Passing a string should fail.

    Location: src/knarr/commerce/schemas.py:194
    Severity: LOW (regression guard)
    """

    def test_adv_024_available_string(self):
        ok, err = validate_punchhole_card({
            "for_node": "abc",
            "for_access_level": "peer",
            "available": "not a list",
            "not_available": [],
        })
        assert not ok

    def test_adv_024_available_dict(self):
        ok, err = validate_punchhole_card({
            "for_node": "abc",
            "for_access_level": "peer",
            "available": {"key": "economy.summary"},
            "not_available": [],
        })
        assert not ok


# ===========================================================================
# ADV-025: BCW SolanaWatcher — parse_positive_amount with bool
# ===========================================================================

class TestAdv025ParseAmountBool:
    """_parse_positive_amount explicitly rejects bools. True/False are
    instances of int in Python, so without the bool check, True would
    parse as 1. Regression guard.

    Location: src/knarr/plugins/10-bcw/solana.py:219
    Severity: LOW (regression guard)
    """

    def test_adv_025_bool_rejected(self):
        assert _bcw_solana.SolanaWatcher._parse_positive_amount(True) is None
        assert _bcw_solana.SolanaWatcher._parse_positive_amount(False) is None


# ===========================================================================
# ADV-026: Schema — cache_object with data = [] (list not dict)
# ===========================================================================

class TestAdv026CacheObjectDataList:
    """validate_cache_object checks isinstance(data, dict).
    Passing a list should fail.

    Location: src/knarr/commerce/schemas.py:205
    Severity: LOW (regression guard)
    """

    def test_adv_026_data_is_list(self):
        ok, err = validate_cache_object({
            "object_key": "economy.summary",
            "data": [1, 2, 3],
            "granularity": {},
        })
        assert not ok


# ===========================================================================
# ADV-027: Dynamic skills — Inf price
# ===========================================================================

class TestAdv027InfPrice:
    """validate_dynamic_skill checks math.isfinite(price).
    Inf should fail.

    Location: src/knarr/cli/config.py:303
    Severity: LOW (regression guard)
    """

    def test_adv_027_inf_price(self):
        from knarr.cli.config import validate_dynamic_skill
        policy = {
            "dynamic_enabled": True,
            "dynamic_allowed_handlers": ["dynamic_facade.py"],
        }
        ok, reason = validate_dynamic_skill(
            "test-skill",
            {"handler": "dynamic_facade.py:handle", "price": float("inf")},
            policy, 0,
        )
        assert not ok, "Inf price should be rejected"

    def test_adv_027_neg_inf_price(self):
        from knarr.cli.config import validate_dynamic_skill
        policy = {
            "dynamic_enabled": True,
            "dynamic_allowed_handlers": ["dynamic_facade.py"],
        }
        ok, reason = validate_dynamic_skill(
            "test-skill",
            {"handler": "dynamic_facade.py:handle", "price": float("-inf")},
            policy, 0,
        )
        assert not ok, "Negative Inf price should be rejected"
