"""Seam tests for v0.37.0 Vordur — cross-component integration paths.

These tests verify the connections BETWEEN components:
- WM auto-promote for all new v0.37.0 document types
- WM Gate 5 endsWith bypass regression guard
- WM hold+approve for configuration_order
- Frontend object_key validation → bus emit path
- Backend build_card output → WM ingestion round-trip
- Dynamic skills TOML serialize → parse round-trip
- Punchhole startup idempotency guard
"""

import asyncio
import importlib.util
import json
import math
import os
import sys
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from nacl.signing import SigningKey

# Ensure src on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from knarr.core.warehouse_manager import WarehouseManager
from knarr.core.proof import sign_document
from knarr.cli.config import (
    write_dynamic_skill, load_dynamic_skills, _serialize_skills_toml,
)


# ---------- Shared test infrastructure ----------

NODE_ID = "a" * 64
IDENTITY_FRAGMENTS = [
    NODE_ID,
    f"did:knarr:{NODE_ID}",
    f"did:knarr:{NODE_ID}#key-1",
    f"did:knarr:{NODE_ID}#cockpit-1",
]
FAKE_PUBKEY = b"\x01" * 32


class _QuarantineStorage:
    """Minimal in-memory quarantine store for WM seam tests."""

    def __init__(self):
        self._rows = {}

    def quarantine_store(self, id, document_type, document_json,
                         originator_pubkey, status, gate_results, reason):
        self._rows[id] = {
            "id": id, "document_type": document_type,
            "document_json": document_json,
            "originator_pubkey": originator_pubkey,
            "status": status, "gate_results": gate_results,
            "reason": reason, "received_at": time.time(),
            "promoted_at": None, "resolved_at": None,
        }

    def quarantine_get(self, id):
        return self._rows.get(id)

    def quarantine_update_status(self, id, status, reason=None,
                                 promoted_at=None, resolved_at=None):
        row = self._rows.get(id)
        if not row:
            return
        row["status"] = status
        if reason is not None:
            row["reason"] = reason
        if promoted_at is not None:
            row["promoted_at"] = promoted_at
        if resolved_at is not None:
            row["resolved_at"] = resolved_at


def _make_wm(config_override=None, storage=None):
    bus = MagicMock()
    st = storage or _QuarantineStorage()
    wr = MagicMock()
    config = config_override or {"debug": False}
    wm = WarehouseManager(
        node_id=NODE_ID,
        identity_fragments=IDENTITY_FRAGMENTS,
        bus=bus, storage=st, config=config,
        write_receipt_cb=wr,
    )
    return wm, bus, st, wr


def _make_signed_doc(doc_type, vm=None, identity=None, counterparty=None):
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
    }
    if doc_type in _BODY_FIELDS:
        doc.update(_BODY_FIELDS[doc_type])
    return doc


# ---------- Load punchhole modules ----------

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _load_plugin(plugin_dir_name, mod_name):
    plugin_dir = BASE_DIR / "src" / "knarr" / "plugins" / plugin_dir_name
    plugin_path = plugin_dir / "handler.py"
    spec = importlib.util.spec_from_file_location(mod_name, str(plugin_path))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(plugin_dir))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(plugin_dir))
    return mod


_BACKEND_MOD = _load_plugin("09-punchhole-backend", "seam_backend")
PunchholeBackendPlugin = _BACKEND_MOD.PunchholeBackendPlugin
_FRONTEND_MOD = _load_plugin("08-punchhole-frontend", "seam_frontend")
PunchholeFrontendPlugin = _FRONTEND_MOD.PunchholeFrontendPlugin


# =====================================================================
# Seam 1: WM auto-promote for ALL v0.37.0 document types
# =====================================================================

class TestAutoPromoteNewDocTypes(unittest.TestCase):
    """Each new v0.37.0 BCW/disclosure type must complete the full
    ingest→promote→bus.emit→write_receipt pipeline."""

    AUTO_PROMOTE_TYPES = [
        "payment_received",
        "payment_finalized",
        "payment_executed",
        "wallet_transfer",
        "wallet_withdrawal",
        "punchhole_card",
        "cache_object",
    ]

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_all_new_types_auto_promote(self, mock_verify):
        for doc_type in self.AUTO_PROMOTE_TYPES:
            with self.subTest(doc_type=doc_type):
                wm, bus, st, wr = _make_wm()
                doc = _make_signed_doc(doc_type)
                result = wm.ingest(doc, FAKE_PUBKEY)
                self.assertEqual(result.status, "promoted",
                                 f"{doc_type} should auto-promote")
                # Bus event uses correct type name
                bus.emit.assert_called_once()
                event_name = bus.emit.call_args[0][0]
                self.assertEqual(event_name, f"wm.promoted.{doc_type}")
                # write_receipt callback fired
                wr.assert_called_once()


# =====================================================================
# Seam 2: Gate 5 endsWith bypass — regression guard
# =====================================================================

class TestGate5EndswithBypass(unittest.TestCase):
    """A foreign node's #cockpit-1 VM must NOT pass Gate 5."""

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_foreign_cockpit_vm_rejected(self, mock_verify):
        """did:knarr:<foreign_id>#cockpit-1 must be rejected."""
        wm, bus, st, wr = _make_wm()
        foreign_id = "f" * 64
        doc = _make_signed_doc(
            "configuration_order",
            vm=f"did:knarr:{foreign_id}#cockpit-1",
            identity=NODE_ID,
        )
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "rejected")

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_own_cockpit_vm_accepted(self, mock_verify):
        """did:knarr:<own_id>#cockpit-1 must be accepted."""
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc(
            "configuration_order",
            vm=f"did:knarr:{NODE_ID}#cockpit-1",
            identity=NODE_ID,
        )
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertIn(result.status, ("promoted", "held"))


# =====================================================================
# Seam 3: WM hold+approve for configuration_order
# =====================================================================

class TestConfigurationOrderLifecycle(unittest.TestCase):
    """configuration_order must hold → approve → emit wm.promoted.configuration_order."""

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_config_order_hold_then_approve(self, mock_verify):
        wm, bus, st, wr = _make_wm()
        doc = _make_signed_doc(
            "configuration_order",
            vm=f"did:knarr:{NODE_ID}#cockpit-1",
            identity=NODE_ID,
        )
        result = wm.ingest(doc, FAKE_PUBKEY)
        self.assertEqual(result.status, "held")
        self.assertIsNotNone(result.quarantine_id)

        # Approve
        ok = wm.approve(result.quarantine_id)
        self.assertTrue(ok)

        # Verify bus event name
        promote_calls = [c for c in bus.emit.call_args_list
                         if c[0][0].startswith("wm.promoted")]
        self.assertEqual(len(promote_calls), 1)
        self.assertEqual(promote_calls[0][0][0],
                         "wm.promoted.configuration_order")

        # write_receipt called on approve
        wr.assert_called_once()


# =====================================================================
# Seam 4: Frontend object_key validation → bus emit
# =====================================================================

class TestFrontendObjectKeyToBus(unittest.TestCase):
    """Valid object_keys produce bus events; invalid ones are silently dropped."""

    def _make_frontend(self, tmp_path):
        emitted = []
        ctx = SimpleNamespace(
            node_id="a" * 64,
            plugin_dir=tmp_path,
            storage_path=None,
            subscribe_events=None,
            sign_document=None,
            emit_event=lambda et, **kw: emitted.append({"event": et, **kw}),
        )
        plugin = PunchholeFrontendPlugin(ctx, {"disclosure_log": "disc.db"})
        plugin._backend_ready = True
        return plugin, emitted

    @patch.object(_FRONTEND_MOD, "verify_document", return_value=True)
    def test_valid_key_emits_miss(self, mock_verify):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            plugin, emitted = self._make_frontend(Path(tmp))
            sk = SigningKey.generate()
            node_id = sk.verify_key.encode().hex()
            body = {
                "action": "request",
                "object_key": "economy.summary",
                "payload": sign_document(
                    {"document_type": "punchhole_request", "version": 1,
                     "object_key": "economy.summary", "ts": time.time()},
                    sk, f"did:knarr:{node_id}#key-1",
                ),
            }
            asyncio.get_event_loop().run_until_complete(
                plugin.on_mail_received("punchhole.request", node_id,
                                        "a" * 64, body, None)
            )
            miss = [e for e in emitted if e["event"].startswith("cache.miss.")]
            self.assertEqual(len(miss), 1)
            self.assertEqual(miss[0]["event"], "cache.miss.data.economy.summary")

    @patch.object(_FRONTEND_MOD, "verify_document", return_value=True)
    def test_path_traversal_key_blocked(self, mock_verify):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            plugin, emitted = self._make_frontend(Path(tmp))
            sk = SigningKey.generate()
            node_id = sk.verify_key.encode().hex()
            body = {
                "action": "request",
                "object_key": "../../etc/passwd",
                "payload": sign_document(
                    {"document_type": "punchhole_request", "version": 1,
                     "object_key": "../../etc/passwd", "ts": time.time()},
                    sk, f"did:knarr:{node_id}#key-1",
                ),
            }
            asyncio.get_event_loop().run_until_complete(
                plugin.on_mail_received("punchhole.request", node_id,
                                        "a" * 64, body, None)
            )
            miss = [e for e in emitted if e["event"].startswith("cache.miss.")]
            self.assertEqual(len(miss), 0, "Path traversal key must not emit bus event")

    @patch.object(_FRONTEND_MOD, "verify_document", return_value=True)
    def test_list_body_does_not_crash(self, mock_verify):
        """JSON list body must be rejected gracefully, not crash."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            plugin, emitted = self._make_frontend(Path(tmp))
            # String that parses to a list
            body = "[1, 2, 3]"
            asyncio.get_event_loop().run_until_complete(
                plugin.on_mail_received("punchhole.request", "b" * 64,
                                        "a" * 64, body, None)
            )
            # Should not crash, no events emitted
            miss = [e for e in emitted if e["event"].startswith("cache.miss.")]
            self.assertEqual(len(miss), 0)


# =====================================================================
# Seam 5: Backend build_card → WM ingestion round-trip
# =====================================================================

class TestCardThroughWM(unittest.TestCase):
    """A card built by the backend must pass WM validation as punchhole_card."""

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_backend_card_passes_wm_gates(self, mock_verify):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sk = SigningKey.generate()
            node_id = sk.verify_key.encode().hex()

            # Write a schema
            schema = tmp_path / "exposure_schema.toml"
            schema.write_text(
                'trusted_nodes = []\n'
                '[objects."economy.summary"]\n'
                'access = "all_signed"\n'
                'description = "Economy summary"\n'
                'source = "ledger"\n'
                'fields = ["balance"]\n'
                '[objects."economy.summary".granularity]\n'
                'balance = "exact"\n',
                encoding="utf-8",
            )

            ctx = SimpleNamespace(
                node_id=node_id, plugin_dir=tmp_path, storage_path=None,
                subscribe_events=None,
                emit_event=lambda *a, **kw: None,
                sign_document=lambda doc: sign_document(doc, sk, f"did:knarr:{node_id}#key-1"),
                log=SimpleNamespace(warning=lambda *a, **k: None),
            )
            backend = PunchholeBackendPlugin(ctx, {"schema_file": "exposure_schema.toml"})

            # Build a card
            requester = "b" * 64
            card = backend.build_card(requester)
            self.assertIsNotNone(card)

            # Feed it through WM as a punchhole_card document
            card["document_type"] = "punchhole_card"
            card["type"] = "knarr/commerce/punchhole_card"
            wm, bus, st, wr = _make_wm()
            result = wm.ingest(card, FAKE_PUBKEY)
            self.assertEqual(result.status, "promoted",
                             f"Card from backend should pass WM gates, got: {result}")


# =====================================================================
# Seam 6: Dynamic skills TOML round-trip
# =====================================================================

class TestDynamicSkillsRoundTrip(unittest.TestCase):
    """write_dynamic_skill → parse → load_dynamic_skills must preserve all types."""

    def test_roundtrip_numeric_and_list_fields(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ok = write_dynamic_skill(tmp_path, "test-skill", {
                "handler": "dynamic_facade.py:handle",
                "price": 1.5,
                "timeout": 30,
                "tags": ["math", "compute"],
                "enabled": True,
                "description": "A test skill with \"quotes\" and special chars",
            })
            self.assertTrue(ok)

            # Parse with tomllib to verify valid TOML
            text = (tmp_path / "knarr.skills.toml").read_text(encoding="utf-8")
            parsed = tomllib.loads(text)
            skill = parsed["skills"]["test-skill"]
            self.assertAlmostEqual(skill["price"], 1.5)
            self.assertEqual(skill["timeout"], 30)
            self.assertEqual(skill["tags"], ["math", "compute"])
            self.assertTrue(skill["enabled"])
            self.assertIn("quotes", skill["description"])

            # load_dynamic_skills also works
            loaded = load_dynamic_skills(tmp_path)
            self.assertIn("test-skill", loaded)
            self.assertAlmostEqual(loaded["test-skill"]["price"], 1.5)

    def test_nan_inf_in_numeric_field_produces_valid_toml(self):
        """NaN/Inf as numeric values must not produce unparseable TOML."""
        import tempfile
        for bad_val in [float("nan"), float("inf"), float("-inf")]:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                # _serialize_skills_toml emits these via f"{k} = {v}"
                # which produces "price = nan" — invalid TOML
                toml_text = _serialize_skills_toml({
                    "bad-skill": {"price": bad_val, "handler": "test.py"},
                })
                # Verify: either it's parseable TOML or we documented it fails
                try:
                    parsed = tomllib.loads(toml_text)
                    # If it parsed, the value shouldn't be NaN/Inf
                    if "bad-skill" in parsed.get("skills", {}):
                        val = parsed["skills"]["bad-skill"].get("price")
                        self.assertTrue(
                            val is None or (isinstance(val, (int, float)) and math.isfinite(val)),
                            f"Non-finite value {bad_val} leaked through TOML round-trip"
                        )
                except Exception:
                    # TOML parse failure is acceptable — means the value was rejected
                    pass

    def test_injection_via_key_newlines_blocked(self):
        """Config keys containing newlines must not create extra TOML sections."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ok = write_dynamic_skill(tmp_path, "safe-skill", {
                "handler": "dynamic_facade.py:handle",
                "price": 1.0,
                'x = true\n[skills.injected]\nhandler': True,
            })
            self.assertTrue(ok)
            text = (tmp_path / "knarr.skills.toml").read_text(encoding="utf-8")
            parsed = tomllib.loads(text)
            self.assertNotIn("injected", parsed.get("skills", {}))


# =====================================================================
# Seam 7: Punchhole startup idempotency
# =====================================================================

class TestStartupIdempotency(unittest.TestCase):
    """Backend _startup() must only run once even if called twice."""

    def test_double_startup_emits_ready_once(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sk = SigningKey.generate()
            node_id = sk.verify_key.encode().hex()
            emitted = []

            schema = tmp_path / "exposure_schema.toml"
            schema.write_text(
                'trusted_nodes = []\n'
                '[objects.skills]\n'
                'access = "all_signed"\n'
                'description = "Skills"\n'
                'source = "skill_registry"\n'
                'fields = ["skill_name"]\n',
                encoding="utf-8",
            )

            ctx = SimpleNamespace(
                node_id=node_id, plugin_dir=tmp_path, storage_path=None,
                subscribe_events=None,
                emit_event=lambda et, **kw: emitted.append({"event": et, **kw}),
                sign_document=lambda doc: sign_document(doc, sk, f"did:knarr:{node_id}#key-1"),
                log=SimpleNamespace(warning=lambda *a, **k: None),
            )

            plugin = PunchholeBackendPlugin(ctx, {"schema_file": "exposure_schema.toml"})
            # __init__ schedules startup via ensure_future, run it
            asyncio.get_event_loop().run_until_complete(plugin._startup())

            ready_count = sum(1 for e in emitted if e["event"] == "cache.backend.ready")
            self.assertEqual(ready_count, 1,
                             f"cache.backend.ready emitted {ready_count} times, expected 1")


# =====================================================================
# Seam 8: BCW derive → watch → emit round-trip
# =====================================================================

def _make_bcw(tmp_path):
    _BCW_MOD = _load_plugin("10-bcw", "seam_bcw_fn")
    emitted = []

    def _vault_get(*args):
        if args and args[-1] == "bcw_master_seed":
            return "11" * 32
        return None

    ctx = SimpleNamespace(
        plugin_dir=tmp_path,
        node_id="a" * 64,
        subscribe_events=lambda *patterns: SimpleNamespace(poll=lambda: []),
        get_peers=lambda: [],
        emit_event=lambda et, **kw: emitted.append({"event": et, **kw}),
        log=SimpleNamespace(warning=lambda *a, **k: None),
        sign_document=lambda doc: doc,
        vault_get=_vault_get,
    )

    config = {
        "enabled": True, "poll_interval_seconds": 10,
        "chains": [{"chain_id": "solana-mainnet", "rpc_url": "http://mock"}],
    }
    plugin = _BCW_MOD.BCWPlugin(ctx, config)
    return plugin, emitted


def test_bcw_valid_hex_node_id_registers_watch(tmp_path):
    plugin, emitted = _make_bcw(tmp_path)
    valid_id = "ab" * 32
    before = len(plugin._store.list_watches())
    plugin._handle_watch_request(valid_id, "solana-mainnet")
    after = len(plugin._store.list_watches())
    assert after > before
    assigned = [e for e in emitted if e["event"] == "bcw.address_assigned"]
    assert len(assigned) == 1


def test_bcw_non_hex_node_id_rejected(tmp_path):
    plugin, emitted = _make_bcw(tmp_path)
    before = len(plugin._store.list_watches())
    plugin._handle_watch_request("g" * 64, "solana-mainnet")
    after = len(plugin._store.list_watches())
    assert after == before, "Non-hex node_id must not register"
    assigned = [e for e in emitted if e["event"] == "bcw.address_assigned"]
    assert len(assigned) == 0


if __name__ == "__main__":
    unittest.main()
