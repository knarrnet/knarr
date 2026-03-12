"""Adversarial tests for v0.37.0 security membrane.

These tests intentionally assert stricter security behavior.
Failures indicate exploitable or fail-open behavior in current code.
"""

from __future__ import annotations

import importlib.util
import math
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nacl.signing import SigningKey

from knarr.cli.config import write_dynamic_skill
from knarr.commerce.schemas import (
    validate_payment_executed,
    validate_payment_received,
    validate_wallet_withdrawal,
)
from knarr.core.proof import sign_document
from knarr.core.warehouse_manager import WarehouseManager


BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _load_plugin_module(plugin_dir_name: str, module_name: str):
    plugin_dir = BASE_DIR / "src" / "knarr" / "plugins" / plugin_dir_name
    plugin_path = plugin_dir / "handler.py"
    spec = importlib.util.spec_from_file_location(module_name, str(plugin_path))
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.path.insert(0, str(plugin_dir))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(plugin_dir))
    return mod


_FRONTEND_MOD = _load_plugin_module("08-punchhole-frontend", "adv_frontend_gpt")
PunchholeFrontendPlugin = _FRONTEND_MOD.PunchholeFrontendPlugin

_BACKEND_MOD = _load_plugin_module("09-punchhole-backend", "adv_backend_gpt")
PunchholeBackendPlugin = _BACKEND_MOD.PunchholeBackendPlugin
_apply_granularity = _BACKEND_MOD._apply_granularity
_tier_has_access = _BACKEND_MOD._tier_has_access

_BCW_MOD = _load_plugin_module("10-bcw", "adv_bcw_gpt")
BCWPlugin = _BCW_MOD.BCWPlugin
derive_counterparty_address = _BCW_MOD.derive_counterparty_address


NODE_ID = "0123456789abcdef" * 4
ORIGINATOR_PUBKEY = b"\x01" * 32


class _QuarantineStorage:
    def __init__(self):
        self._rows: dict[str, dict[str, Any]] = {}

    def quarantine_store(self, id, document_type, document_json, originator_pubkey, status, gate_results, reason):
        self._rows[id] = {
            "id": id,
            "document_type": document_type,
            "document_json": document_json,
            "originator_pubkey": originator_pubkey,
            "status": status,
            "gate_results": gate_results,
            "reason": reason,
            "received_at": time.time(),
            "promoted_at": None,
            "resolved_at": None,
        }

    def quarantine_get(self, id):
        return self._rows.get(id)

    def quarantine_update_status(self, id, status, reason=None, promoted_at=None, resolved_at=None):
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


def _make_wm() -> WarehouseManager:
    bus = SimpleNamespace(emit=lambda *args, **kwargs: None)
    storage = _QuarantineStorage()
    return WarehouseManager(
        node_id=NODE_ID,
        identity_fragments=[
            NODE_ID,
            f"did:knarr:{NODE_ID}",
            f"did:knarr:{NODE_ID}#key-1",
            f"did:knarr:{NODE_ID}#cockpit-1",
        ],
        bus=bus,
        storage=storage,
        config={"debug": False},
        write_receipt_cb=lambda **kwargs: None,
    )


def _proof(vm: Any, created: str | None = None) -> dict:
    return {
        "type": "DataIntegrityProof",
        "cryptosuite": "eddsa-jcs-2022",
        "verificationMethod": vm,
        "proofPurpose": "assertionMethod",
        "created": created or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "proofValue": "z" + "A" * 86,
    }


def _credit_note_doc(identity: str, counterparty: str, vm: Any) -> dict:
    return {
        "document_type": "credit_note",
        "type": "knarr/commerce/credit_note",
        "identity": identity,
        "counterparty": counterparty,
        "amount": 1.0,
        "reason": "other",
        "timestamp": time.time(),
        "references": {"task_id": "task-1"},
        "proof": _proof(vm),
    }


def _configuration_order_doc(vm: Any, identity: str | None = None) -> dict:
    return {
        "document_type": "configuration_order",
        "identity": identity or NODE_ID,
        "target": "exposure_schema",
        "operation": "upsert_object",
        "changes": {"object_key": "economy.summary"},
        "proof": _proof(vm),
    }


def _signed_request(sk: SigningKey, node_id: str, object_key: str) -> dict:
    payload = {
        "document_type": "punchhole_request",
        "version": 1,
        "object_key": object_key,
        "ts": time.time(),
    }
    return sign_document(payload, sk, f"did:knarr:{node_id}#key-1")


class _NoSubCtx:
    def __init__(self, plugin_dir: Path):
        self.node_id = "a" * 64
        self.plugin_dir = plugin_dir
        self.storage_path = None
        self.subscribe_events = None
        self.sign_document = None
        self.emitted: list[dict] = []

    def emit_event(self, event_type: str, **fields):
        self.emitted.append({"event": event_type, **fields})


# ---------------------------------------------------------------------------
# A. Warehouse Manager fail-closed and auth bypass
# ---------------------------------------------------------------------------


def test_adv_001_gate3_validator_exception_must_reject(monkeypatch):
    wm = _make_wm()
    monkeypatch.setattr("knarr.core.warehouse_manager.verify_document", lambda *_: True)

    def _boom(_body):
        raise RuntimeError("validator exploded")

    wm._validators = {"credit_note": _boom}
    doc = _credit_note_doc(identity=NODE_ID, counterparty="b" * 64, vm=f"did:knarr:{'b'*64}#key-1")

    result = wm.ingest(doc, ORIGINATOR_PUBKEY)
    assert result.status == "rejected"
    assert result.gate_results.get(3) == "fail"


def test_adv_002_gate3_non_dict_body_must_reject_not_crash(monkeypatch):
    wm = _make_wm()
    monkeypatch.setattr("knarr.core.warehouse_manager.verify_document", lambda *_: True)

    doc = {
        "document_type": "payment_received",
        "body": None,
        "proof": _proof(f"did:knarr:{'b'*64}#key-1"),
    }

    result = wm.ingest(doc, ORIGINATOR_PUBKEY)
    assert result.status == "rejected"
    assert result.gate_results.get(3) == "fail"


def test_adv_003_gate5_none_vm_must_reject_not_crash(monkeypatch):
    wm = _make_wm()
    monkeypatch.setattr("knarr.core.warehouse_manager.verify_document", lambda *_: True)

    doc = _configuration_order_doc(vm=None, identity=NODE_ID)
    result = wm.ingest(doc, ORIGINATOR_PUBKEY)
    assert result.status == "rejected"
    assert result.gate_results.get(5) == "fail"


def test_adv_004_gate5_requires_exact_cockpit_fragment(monkeypatch):
    wm = _make_wm()
    monkeypatch.setattr("knarr.core.warehouse_manager.verify_document", lambda *_: True)

    doc = _configuration_order_doc(vm=f"did:knarr:{NODE_ID}#cockpit-1-fake", identity=NODE_ID)
    result = wm.ingest(doc, ORIGINATOR_PUBKEY)
    assert result.status == "rejected"


def test_adv_005_gate2_must_not_accept_vm_substring_spoof(monkeypatch):
    wm = _make_wm()
    monkeypatch.setattr("knarr.core.warehouse_manager.verify_document", lambda *_: True)

    spoof_vm = f"did:knarr:{'f'*64}#key-1?hint={NODE_ID}"
    doc = _credit_note_doc(identity="c" * 64, counterparty="d" * 64, vm=spoof_vm)
    result = wm.ingest(doc, ORIGINATOR_PUBKEY)
    assert result.status == "rejected"
    assert result.gate_results.get(2) == "fail"


# ---------------------------------------------------------------------------
# F. Punchhole backend ACL/granularity fail-open
# ---------------------------------------------------------------------------


def test_adv_006_unknown_required_acl_must_deny():
    assert _tier_has_access("all_signed", "super_admin") is False


def test_adv_007_unknown_access_object_must_not_be_available(tmp_path):
    schema = tmp_path / "exposure_schema.toml"
    schema.write_text(
        "\n".join(
            [
                "trusted_nodes = []",
                '[objects."secret.object"]',
                'access = "super_admin"',
                'description = "secret"',
                'source = "skill_registry"',
                'fields = ["x"]',
            ]
        ),
        encoding="utf-8",
    )

    ctx = SimpleNamespace(
        node_id="a" * 64,
        plugin_dir=tmp_path,
        storage_path=None,
        subscribe_events=None,
        emit_event=lambda *args, **kwargs: None,
        sign_document=lambda doc: {**doc, "proof": {"type": "fake"}},
        log=SimpleNamespace(warning=lambda *a, **k: None),
    )
    plugin = PunchholeBackendPlugin(ctx, {"schema_file": "exposure_schema.toml"})
    card = plugin.build_card("b" * 64)
    assert card is not None

    available = {row["key"] for row in card["available"]}
    not_available = {row["key"] for row in card["not_available"]}
    assert "secret.object" not in available
    assert "secret.object" in not_available


def test_adv_008_range_inf_control_must_not_return_nan():
    out = _apply_granularity(10.0, "range:inf")
    assert out is None or (isinstance(out, (int, float)) and math.isfinite(out))


def test_adv_009_range_nan_control_must_reject():
    out = _apply_granularity(10.0, "range:nan")
    assert out is None


def test_adv_010_range_negative_control_must_reject():
    out = _apply_granularity(10.0, "range:-1")
    assert out is None


# ---------------------------------------------------------------------------
# E. Punchhole frontend object_key sanitization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adv_011_frontend_rejects_path_traversal_object_key(tmp_path, monkeypatch):
    sk = SigningKey.generate()
    node_id = sk.verify_key.encode().hex()

    ctx = _NoSubCtx(tmp_path)
    plugin = PunchholeFrontendPlugin(ctx, {"disclosure_log": "disc.db"})
    plugin._backend_ready = True

    monkeypatch.setattr(_FRONTEND_MOD, "verify_document", lambda *_: True)

    body = {
        "action": "request",
        "object_key": "../../etc/passwd",
        "payload": _signed_request(sk, node_id, "../../etc/passwd"),
    }
    await plugin.on_mail_received("punchhole.request", node_id, ctx.node_id, body, None)

    miss = [e for e in ctx.emitted if e["event"].startswith("cache.miss.")]
    assert miss == []


@pytest.mark.asyncio
async def test_adv_012_frontend_rejects_sqlish_object_key(tmp_path, monkeypatch):
    sk = SigningKey.generate()
    node_id = sk.verify_key.encode().hex()

    ctx = _NoSubCtx(tmp_path)
    plugin = PunchholeFrontendPlugin(ctx, {"disclosure_log": "disc2.db"})
    plugin._backend_ready = True

    monkeypatch.setattr(_FRONTEND_MOD, "verify_document", lambda *_: True)

    key = "skills;DROP TABLE ledger;--"
    body = {
        "action": "request",
        "object_key": key,
        "payload": _signed_request(sk, node_id, key),
    }
    await plugin.on_mail_received("punchhole.request", node_id, ctx.node_id, body, None)

    miss = [e for e in ctx.emitted if e["event"].startswith("cache.miss.")]
    assert miss == []


# ---------------------------------------------------------------------------
# G. BCW node_id validation
# ---------------------------------------------------------------------------


def _bcw_ctx(tmp_path: Path):
    sub = SimpleNamespace(poll=lambda: [])

    def _vault_get(*args):
        if args and args[-1] == "bcw_master_seed":
            return "11" * 32
        return None

    emitted: list[dict] = []

    def _emit(event_type: str, **fields):
        emitted.append({"event": event_type, **fields})

    ctx = SimpleNamespace(
        plugin_dir=tmp_path,
        node_id="a" * 64,
        subscribe_events=lambda *patterns: sub,
        get_peers=lambda: [],
        emit_event=_emit,
        log=SimpleNamespace(warning=lambda *a, **k: None),
        sign_document=lambda doc: doc,
        vault_get=_vault_get,
    )
    ctx._emitted = emitted
    return ctx


def test_adv_013_derive_counterparty_address_rejects_non_hex_node_id():
    with pytest.raises(ValueError):
        derive_counterparty_address(b"\x01" * 32, "g" * 64, "solana-mainnet")


def test_adv_014_watch_request_rejects_non_hex_node_id(tmp_path):
    config = {
        "enabled": True,
        "poll_interval_seconds": 10,
        "chains": [{"chain_id": "solana-mainnet", "rpc_url": "http://mock-rpc"}],
    }
    ctx = _bcw_ctx(tmp_path)
    plugin = BCWPlugin(ctx, config)

    before = len(plugin._store.list_watches())
    plugin._handle_watch_request("g" * 64, "solana-mainnet")
    after = len(plugin._store.list_watches())

    assert after == before
    assert [e for e in ctx._emitted if e["event"] == "bcw.address_assigned"] == []


# ---------------------------------------------------------------------------
# I. Dynamic skills TOML injection
# ---------------------------------------------------------------------------


def test_adv_015_write_dynamic_skill_rejects_section_injection(tmp_path):
    ok = write_dynamic_skill(
        tmp_path,
        "evil]\n[skills.pwned",
        {
            "handler": "skills/dynamic_facade.py:handle",
            "price": 1.0,
        },
    )
    assert ok is True
    text = (tmp_path / "knarr.skills.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert "pwned" not in parsed.get("skills", {})


def test_adv_016_write_dynamic_skill_rejects_unescaped_string_values(tmp_path):
    ok = write_dynamic_skill(
        tmp_path,
        "safe-skill",
        {
            "handler": "skills/dynamic_facade.py:handle",
            "price": 1.0,
            'x = true\n[skills.injected]\nhandler': True,
        },
    )
    assert ok is True
    text = (tmp_path / "knarr.skills.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert "injected" not in parsed.get("skills", {})


# ---------------------------------------------------------------------------
# H. Document validator strictness gaps
# ---------------------------------------------------------------------------


def test_adv_017_payment_received_requires_dict_confirmation():
    ok, _ = validate_payment_received(
        {
            "chain_id": "solana-mainnet",
            "tx_hash": "abc",
            "tx_index": 0,
            "from_address": "S",
            "to_address": "R",
            "amount": 10,
            "denom": "SOL",
            "decimals": 9,
            "confirmation": "finalized",
        }
    )
    assert ok is False


def test_adv_018_payment_executed_requires_dict_finality():
    ok, _ = validate_payment_executed(
        {
            "chain_id": "solana-mainnet",
            "tx_hash": "abc",
            "from_address": "S",
            "to_address": "R",
            "amount": 10,
            "denom": "SOL",
            "decimals": 9,
            "settlement_ref": {},
            "finality": "finalized",
        }
    )
    assert ok is False


def test_adv_019_wallet_withdrawal_requires_numeric_decimals():
    ok, _ = validate_wallet_withdrawal(
        {
            "chain_id": "solana-mainnet",
            "tx_hash": "abc",
            "from_address": "S",
            "to_address": "R",
            "amount": 10,
            "denom": "SOL",
            "decimals": "9",
        }
    )
    assert ok is False
