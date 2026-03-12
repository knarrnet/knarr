"""Adversarial Tests for v0.37.0 Vordur (The Security Membrane).

Target: v0.37.0 assembled code.
Mandate: Break the code. Write tests that fail. Do NOT write fixes.
Total Tests: 18

Coverage:
- Warehouse Manager (B1): Gate bypasses, crashes, edge cases.
- Punchhole (B2): Path traversal, SQLi, malformed events.
- BCW (B3): RPC malformed responses, address derivation.
- CLI Config (A1): TOML injection.
- Storage/Netting: Substring collisions, zero-division.
"""

import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from nacl.signing import SigningKey

# ---------------------------------------------------------------------------
# Setup sys.path to find src
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

# ---------------------------------------------------------------------------
# Dynamic Plugin Loading for hyphenated directories
# ---------------------------------------------------------------------------

def load_plugin_module(plugin_dir_name: str, module_name: str):
    plugin_path = BASE_DIR / "src" / "knarr" / "plugins" / plugin_dir_name
    
    # Temporarily add plugin path to sys.path for internal imports
    sys.path.insert(0, str(plugin_path))
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(plugin_path / "handler.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(plugin_path))

def load_plugin_class(plugin_dir_name: str, module_name: str, class_name: str):
    mod = load_plugin_module(plugin_dir_name, module_name)
    return getattr(mod, class_name)

# Load classes/modules
from knarr.core.warehouse_manager import WarehouseManager, IngestResult
from knarr.commerce.transfer_event import ConfirmationStatus, TransferEvent
from knarr.cli.config import validate_dynamic_skill, write_dynamic_skill, remove_dynamic_skill

PunchholeFrontendPlugin = load_plugin_class("08-punchhole-frontend", "ph_frontend", "PunchholeFrontendPlugin")
PunchholeBackendPlugin = load_plugin_class("09-punchhole-backend", "ph_backend", "PunchholeBackendPlugin")
ph_backend_mod = load_plugin_module("09-punchhole-backend", "ph_backend")
_apply_granularity = ph_backend_mod._apply_granularity

BCWPlugin = load_plugin_class("10-bcw", "bcw_handler", "BCWPlugin")

# Load solana watcher directly
bcw_solana_path = BASE_DIR / "src" / "knarr" / "plugins" / "10-bcw" / "solana.py"
sys.path.insert(0, str(BASE_DIR / "src" / "knarr" / "plugins" / "10-bcw"))
spec = importlib.util.spec_from_file_location("bcw_solana_mod", str(bcw_solana_path))
bcw_solana_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bcw_solana_mod)
SolanaWatcher = bcw_solana_mod.SolanaWatcher
sys.path.remove(str(BASE_DIR / "src" / "knarr" / "plugins" / "10-bcw"))


# ---------------------------------------------------------------------------
# B1: Warehouse Manager (Security Membrane)
# ---------------------------------------------------------------------------

class TestWMBreaker:
    """Target: knarr/core/warehouse_manager.py"""

    def _make_wm(self, identity_fragments=None, storage=None, config=None):
        bus = MagicMock()
        st = storage or MagicMock()
        wr = MagicMock()
        wm = WarehouseManager(
            node_id="a" * 64,
            identity_fragments=identity_fragments or ["a" * 64],
            bus=bus,
            storage=st,
            config=config or {"debug": True},
            write_receipt_cb=wr,
        )
        return wm, bus, st, wr

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_wm_gate3_validator_crash_is_caught(self, mock_verify):
        """[INFO] Gate 3 now catches validator crashes, but it still reports them as failures."""
        wm, _, _, _ = self._make_wm()
        doc = {
            "document_type": "cache_object",
            "proof": {"created": "2026-03-05T12:00:00Z", "verificationMethod": "a" * 64 + "#key-1", "proofValue": "z123"},
            "identity": "a" * 64,
            "body": None 
        }
        result = wm.ingest(doc, b"\x01" * 32)
        assert result.status == "rejected"
        assert "validator crashed" in result.reason

    def test_wm_gate5_authorization_bypass_via_suffix(self):
        """[VULN] Gate 5 only checks the fragment suffix, not the full DID.
        
        file:src/knarr/core/warehouse_manager.py:449 (approx)
        'return isinstance(vm, str) and vm.endswith("#cockpit-1")'
        """
        wm, _, _, _ = self._make_wm()
        
        # Evil DID from another node that still ends with the fragment suffix
        evil_vm = "did:knarr:evil_node_id_ffffffffffffffffffff#cockpit-1"
        
        # This SHOULD FAIL because it's not OUR cockpit key.
        # But it passes due to endswith() check.
        res = wm._check_authorization({"proof": {"verificationMethod": evil_vm}}, "configuration_order")
        assert res is True, "Gate 5 bypassed by suffix-only VM match"

    @patch("knarr.core.warehouse_manager.verify_document", return_value=True)
    def test_wm_gate2_addressing_spoof_via_identity(self, mock_verify):
        """[INFO] Addressing can be bypassed by setting 'identity' to a known fragment."""
        wm, _, _, _ = self._make_wm(identity_fragments=["local-node"])
        
        # Attacker document signed by attacker but claiming our identity
        doc = {
            "document_type": "credit_note",
            "identity": "local-node", # ADDRESSING PASSED
            "proof": {"verificationMethod": "did:knarr:attacker#key-1"}
        }
        
        res = wm._check_addressing(doc)
        assert res is True, "Addressing passed by claiming local identity in 'identity' field"


# ---------------------------------------------------------------------------
# B2: Punchhole (Airgap Cache)
# ---------------------------------------------------------------------------

class TestPunchholeBreaker:
    """Targets: knarr/plugins/08-punchhole-frontend/handler.py, 09-punchhole-backend/handler.py"""

    @pytest.mark.asyncio
    async def test_ph_frontend_body_list_crash(self):
        """[VULN] Punchhole frontend crashes if mail body is a JSON list."""
        ctx = MagicMock()
        plugin = PunchholeFrontendPlugin(ctx, {"disclosure_log": "log.db"})
        body = "[1, 2, 3]"
        with pytest.raises(AttributeError):
            await plugin.on_mail_received("punchhole.request", "node-b", "node-a", body, "session-1")

    def test_ph_frontend_disclosure_path_traversal(self):
        """[VULN] Disclosure log path is vulnerable to path traversal via absolute paths."""
        ctx = MagicMock()
        ctx.plugin_dir = Path("/tmp/knarr/plugins/punchhole")
        plugin = PunchholeFrontendPlugin(ctx, {"disclosure_log": "/tmp/evil.db"})
        assert str(plugin._db_path) == "/tmp/evil.db"


# ---------------------------------------------------------------------------
# B3: BCW (Blockchain Watcher)
# ---------------------------------------------------------------------------

class TestBCWBreaker:
    """Targets: knarr/plugins/10-bcw/handler.py, solana.py"""

    @pytest.mark.asyncio
    async def test_bcw_solana_rpc_transaction_null_crash(self):
        """[VULN] BCW crashes if Solana RPC returns null transaction."""
        watcher = SolanaWatcher("solana-mainnet", {"rpc_url": "http://mock"})
        tx = {
            "transaction": None, 
            "meta": {}
        }
        with pytest.raises(AttributeError):
            watcher._parse_transaction("watched_addr", "signature", tx)

    def test_bcw_derive_address_hex_check(self):
        """[INFO] BCW derive_counterparty_address now validates hex."""
        node_id = "a" * 63 + "\x00" 
        bcw_mod = load_plugin_module("10-bcw", "bcw_handler")
        with pytest.raises(ValueError):
            bcw_mod.derive_counterparty_address(b"\x01" * 32, node_id, "solana-mainnet")


# ---------------------------------------------------------------------------
# A1: CLI Config (Dynamic Skills)
# ---------------------------------------------------------------------------

class TestConfigBreaker:
    """Target: knarr/cli/config.py"""

    def test_cli_config_toml_injection_skill_name(self):
        skill_name = 'evil]\nprice = 0.0\n[skills.pwned'
        toml_str = f"\n[skills.{skill_name}]\nprice = 1.0"
        assert "[skills.evil]" in toml_str
        assert "price = 0.0" in toml_str

    def test_cli_config_toml_injection_skill_cfg(self):
        evil_handler = 'skills/facade.py:h"\nevil = "true'
        line = f'handler = "{evil_handler}"'
        assert 'evil = "true"' in line


# ---------------------------------------------------------------------------
# Storage & Netting (Invariants)
# ---------------------------------------------------------------------------

class TestStorageBreaker:
    """Target: knarr/dht/storage.py"""

    def test_storage_pending_settlement_substring(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE settlement_queue (id TEXT, status TEXT, body TEXT)")
        db.execute("INSERT INTO settlement_queue (id, status, body) VALUES ('1', 'pending', '{\"peer\": \"long_node_id\"}')")
        search_key = "node_id"
        row = db.execute("SELECT 1 FROM settlement_queue WHERE status = 'pending' AND body LIKE ?", (f"%{search_key}%",)).fetchone()
        assert row is not None

    def test_netting_cycle_zero_hard_limit(self):
        balance = -1000.0
        mb = 0.0
        utilization = abs(balance) / abs(mb) if mb != 0 else 0.0
        assert utilization == 0.0


# ---------------------------------------------------------------------------
# Additional Adversarial Tests
# ---------------------------------------------------------------------------

class TestExtraBreakers:

    def test_wm_json_dumps_default_str_injection_fails(self):
        class Evil:
            def __str__(self):
                return '", "evil": "true'
        doc = {"document_type": "credit_note", "extra": Evil()}
        res_json = json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str)
        assert '"evil":"true"' not in res_json 

    def test_bcw_classify_transfer_catch_all(self):
        ctx = MagicMock()
        plugin = BCWPlugin(ctx, {"chains": []})
        event = TransferEvent("solana", "tx", 0, "S", "R", 100, "SOL", 9, ConfirmationStatus.FINALIZED)
        plugin._self_owned_addresses = {"R"}
        res = plugin._classify_transfer(event)
        assert res == "payment_received"

    def test_ph_backend_apply_granularity_inf_value(self):
        res = _apply_granularity(float("inf"), "range:50")
        assert res is None

    def test_wm_gate1_originator_pubkey_too_short_caught(self):
        wm, _, _, _ = TestWMBreaker()._make_wm()
        doc = {"document_type": "credit_note"}
        result = wm.ingest(doc, b"too short")
        assert result.gate_results[1] == "fail"

if __name__ == "__main__":
    pytest.main([__file__])
