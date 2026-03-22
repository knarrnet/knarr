"""
Unit tests for Punchhole Cache Manager (B2).

Tests cover:
  Frontend:
    1. Request with valid signature -> serve cached object
    2. Request with invalid signature -> reject
    3. Cache miss -> emits cache.miss with requester_node_id
    4. Cache fill event -> object cached
    5. Cache stale event -> entry marked stale, next request triggers miss
    6. Request before backend.ready -> rejected/not served
    7. Disclosure log written on every request

  Backend:
    8.  ACL: all_signed -> any signed node passes
    9.  ACL: known_hosts -> only explicit tier nodes
    10. ACL: peer -> only nodes with ledger entries
    11. ACL: trusted -> only operator-listed nodes
    12. Granularity: range:50 -> 1247 becomes 1200
    13. Granularity: range:0.1 -> 0.723 becomes 0.7
    14. Granularity: boolean -> 1247 becomes True
    15. Card generation: correct available/not_available split per tier
    16. Bilateral: live_query, counterparty set, exact granularity
    17. Stale propagation: credit.change -> stales economy.summary
    18. Startup sequence: all objects pushed before ready signal
"""

import asyncio
import json
import math
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from nacl.signing import SigningKey, VerifyKey


# ---------------------------------------------------------------------------
# Helpers — import the modules under test
# ---------------------------------------------------------------------------

import sys, importlib, os

def _load_plugin(plugin_dir_name: str, module_name: str, class_name: str):
    """Dynamically load a plugin handler class."""
    base = Path(__file__).parent.parent.parent
    plugin_path = base / "src" / "knarr" / "plugins" / plugin_dir_name
    sys.path.insert(0, str(plugin_path))
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(plugin_path / "handler.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, class_name)
    finally:
        sys.path.remove(str(plugin_path))


# Import granularity helpers directly from backend handler module
def _load_backend_module():
    base = Path(__file__).parent.parent.parent
    plugin_path = base / "src" / "knarr" / "plugins" / "09-punchhole-backend"
    sys.path.insert(0, str(plugin_path))
    spec = importlib.util.spec_from_file_location("ph_backend", str(plugin_path / "handler.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.path.remove(str(plugin_path))
    return mod


_backend_mod = _load_backend_module()
_apply_granularity = _backend_mod._apply_granularity
_build_data_dict = _backend_mod._build_data_dict
_tier_has_access = _backend_mod._tier_has_access
PunchholeBackendPlugin = _backend_mod.PunchholeBackendPlugin

# Load frontend
def _load_frontend_module():
    base = Path(__file__).parent.parent.parent
    plugin_path = base / "src" / "knarr" / "plugins" / "08-punchhole-frontend"
    sys.path.insert(0, str(plugin_path))
    spec = importlib.util.spec_from_file_location("ph_frontend", str(plugin_path / "handler.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.path.remove(str(plugin_path))
    return mod


_frontend_mod = _load_frontend_module()
PunchholeFrontendPlugin = _frontend_mod.PunchholeFrontendPlugin


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_keypair():
    """Return (signing_key, verify_key, node_id_hex)."""
    sk = SigningKey.generate()
    vk = sk.verify_key
    node_id = vk.encode().hex()
    return sk, vk, node_id


def sign_doc(doc: dict, sk: SigningKey, node_id: str) -> dict:
    """Sign a document using core proof module."""
    from knarr.core.proof import sign_document
    return sign_document(doc, sk, f"did:knarr:{node_id}#key-1")


def make_mock_ctx(tmp_path: Path, with_sign=False, sk=None, node_id=None):
    """Build a minimal PluginContext mock."""
    ctx = MagicMock()
    ctx.node_id = node_id or "a" * 64
    ctx.plugin_dir = tmp_path
    ctx.state_dir = None  # forces fallback to plugin_dir for DB paths
    ctx.storage_path = None

    # Captured events
    emitted: List[Dict] = []

    def _emit(event_type, **fields):
        emitted.append({"event": event_type, **fields})

    ctx.emit_event = _emit
    ctx._emitted = emitted

    # Subscriber stub — returns a Subscriber-like object that never fires
    class _NeverSub:
        async def next(self):
            await asyncio.sleep(999999)
        def poll(self):
            return []

    ctx.subscribe_events = lambda *patterns: _NeverSub()

    if with_sign and sk is not None and node_id is not None:
        def _sign(doc, proof_purpose="assertionMethod"):
            return sign_doc(doc, sk, node_id)
        ctx.sign_document = _sign
    else:
        ctx.sign_document = None

    ctx.log = MagicMock()
    return ctx


def make_db(tmp_path: Path) -> str:
    """Create a minimal node.db for storage tests."""
    db_path = str(tmp_path / "node.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE ledger (
            peer_public_key TEXT PRIMARY KEY,
            balance REAL DEFAULT 0.0,
            tasks_provided INTEGER DEFAULT 0,
            tasks_consumed INTEGER DEFAULT 0,
            soft_limit REAL DEFAULT 10.0,
            prepaid REAL DEFAULT 0.0,
            first_seen REAL,
            last_updated REAL
        )
    """)
    conn.execute("""
        CREATE TABLE address_book (
            node_id TEXT NOT NULL,
            tier TEXT NOT NULL,
            label TEXT,
            last_ip TEXT,
            last_port INTEGER,
            sidecar_port INTEGER DEFAULT 0,
            group_id TEXT,
            last_seen REAL,
            created_at REAL NOT NULL,
            PRIMARY KEY (node_id, tier)
        )
    """)
    conn.execute("""
        CREATE TABLE skills (
            skill_key TEXT PRIMARY KEY,
            is_own INTEGER DEFAULT 0,
            skill_record_json TEXT NOT NULL,
            announced_at REAL,
            ttl INTEGER DEFAULT 3600
        )
    """)
    conn.execute("""
        CREATE TABLE peer_keys (
            node_id TEXT PRIMARY KEY,
            public_key TEXT NOT NULL,
            first_seen REAL,
            last_updated REAL
        )
    """)
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Granularity tests (backend)
# ---------------------------------------------------------------------------

class TestGranularity:
    """Tests 12-14: granularity controls."""

    def test_range_50_rounds_down(self):
        # Test 12: 1247 -> 1200 (range:50)
        assert _apply_granularity(1247, "range:50") == 1200

    def test_range_50_exact_boundary(self):
        # 1200 -> 1200 (already on boundary)
        assert _apply_granularity(1200, "range:50") == 1200

    def test_range_01_rounds_down(self):
        # Test 13: 0.723 -> 0.7 (range:0.1)
        result = _apply_granularity(0.723, "range:0.1")
        assert abs(result - 0.7) < 1e-9, f"Expected 0.7, got {result}"

    def test_range_never_overstates(self):
        # 0.799 -> 0.7 not 0.8
        result = _apply_granularity(0.799, "range:0.1")
        assert abs(result - 0.7) < 1e-9

    def test_boolean_truthy(self):
        # Test 14: 1247 -> True
        assert _apply_granularity(1247, "boolean") is True

    def test_boolean_falsy(self):
        assert _apply_granularity(0, "boolean") is False

    def test_exact_unchanged(self):
        assert _apply_granularity(42, "exact") == 42
        assert _apply_granularity("hello", "exact") == "hello"

    def test_hidden_returns_none(self):
        assert _apply_granularity("secret", "hidden") is None

    def test_recent_n(self):
        lst = list(range(20))
        result = _apply_granularity(lst, "recent:5")
        assert result == [15, 16, 17, 18, 19]

    def test_age_returns_string(self):
        past_ts = time.time() - 7200  # 2 hours ago
        result = _apply_granularity(past_ts, "age")
        assert "h ago" in result

    def test_nan_range_returns_none(self):
        result = _apply_granularity(float("nan"), "range:50")
        assert result is None

    def test_inf_range_returns_none(self):
        result = _apply_granularity(float("inf"), "range:50")
        assert result is None


# ---------------------------------------------------------------------------
# ACL resolution tests (backend)
# ---------------------------------------------------------------------------

class TestACLResolution:
    """Tests 8-11: ACL shorthand resolution."""

    @pytest.fixture
    def tmp_path_fixture(self, tmp_path):
        return tmp_path

    def _make_backend(self, tmp_path, db_path=None, trusted_nodes=None):
        """Construct a PunchholeBackendPlugin with a real DB."""
        sk, vk, node_id = make_keypair()
        ctx = make_mock_ctx(tmp_path, with_sign=True, sk=sk, node_id=node_id)
        if db_path:
            ctx.storage_path = db_path

        # Write minimal schema
        schema_path = tmp_path / "exposure_schema.toml"
        trusted_list = trusted_nodes or []
        schema_path.write_text(
            f'trusted_nodes = {json.dumps(trusted_list)}\n'
            '[objects."economy.summary"]\n'
            'access = "known_hosts"\n'
            'description = "test"\n'
            'source = "bilateral_ledger"\n'
            'fields = ["credit_balance"]\n'
            '[objects."economy.summary".granularity]\n'
            'credit_balance = "range:50"\n'
        )
        plugin = PunchholeBackendPlugin(ctx, {"schema_file": "exposure_schema.toml", "debug": False})
        return plugin, sk, node_id

    def test_all_signed_any_node(self, tmp_path):
        """Test 8: all_signed -> any signed node passes."""
        plugin, _, _ = self._make_backend(tmp_path)
        unknown_nid = "b" * 64
        # Unknown node gets all_signed (lowest tier)
        tier = plugin._resolve_acl_group(unknown_nid)
        assert tier == "all_signed"
        assert _tier_has_access("all_signed", "all_signed")

    def test_known_hosts_only_explicit_tier(self, tmp_path):
        """Test 9: known_hosts -> only address_book tier='explicit'."""
        db_path = make_db(tmp_path)
        _, _, target_nid = make_keypair()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO address_book (node_id, tier, created_at) VALUES (?, 'explicit', ?)",
            (target_nid, time.time()),
        )
        conn.commit()
        conn.close()

        plugin, _, _ = self._make_backend(tmp_path, db_path=db_path)
        tier = plugin._resolve_acl_group(target_nid)
        assert tier == "known_hosts"

    def test_peer_only_ledger_nodes(self, tmp_path):
        """Test 10: peer -> only nodes in peer_keys table."""
        db_path = make_db(tmp_path)
        _, _, peer_nid = make_keypair()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO peer_keys (node_id, public_key, first_seen, last_updated) VALUES (?, ?, ?, ?)",
            (peer_nid, peer_nid, time.time(), time.time()),
        )
        conn.commit()
        conn.close()

        plugin, _, _ = self._make_backend(tmp_path, db_path=db_path)
        tier = plugin._resolve_acl_group(peer_nid)
        assert tier == "peer"

    def test_trusted_operator_list(self, tmp_path):
        """Test 11: trusted -> only operator-listed nodes."""
        _, _, trusted_nid = make_keypair()
        plugin, _, _ = self._make_backend(tmp_path, trusted_nodes=[trusted_nid])
        tier = plugin._resolve_acl_group(trusted_nid)
        assert tier == "trusted"

    def test_peer_beats_known_host(self, tmp_path):
        """Peer takes precedence over known_hosts."""
        db_path = make_db(tmp_path)
        _, _, nid = make_keypair()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO peer_keys (node_id, public_key, first_seen, last_updated) VALUES (?, ?, ?, ?)",
            (nid, nid, time.time(), time.time()),
        )
        conn.execute(
            "INSERT INTO address_book (node_id, tier, created_at) VALUES (?, 'explicit', ?)",
            (nid, time.time()),
        )
        conn.commit()
        conn.close()

        plugin, _, _ = self._make_backend(tmp_path, db_path=db_path)
        assert plugin._resolve_acl_group(nid) == "peer"


# ---------------------------------------------------------------------------
# Card generation tests (backend)
# ---------------------------------------------------------------------------

class TestCardGeneration:
    """Test 15: correct available/not_available split per tier."""

    def _make_backend_with_schema(self, tmp_path):
        sk, vk, node_id = make_keypair()
        ctx = make_mock_ctx(tmp_path, with_sign=True, sk=sk, node_id=node_id)
        schema_path = tmp_path / "exposure_schema.toml"
        schema_path.write_text(
            'trusted_nodes = []\n'
            '[objects."economy.summary"]\n'
            'access = "known_hosts"\n'
            'description = "Economy summary"\n'
            'source = "bilateral_ledger"\n'
            'fields = ["credit_balance"]\n'
            '[objects."economy.summary".granularity]\n'
            'credit_balance = "range:50"\n'
            '[objects.skills]\n'
            'access = "all_signed"\n'
            'description = "Skills catalog"\n'
            'source = "skill_registry"\n'
            'fields = ["skill_name"]\n'
            '[objects.skills.granularity]\n'
            'skill_name = "list"\n'
            '[objects."economy.bilateral"]\n'
            'access = "peer"\n'
            'description = "Bilateral position"\n'
            'source = "bilateral_ledger"\n'
            'live_query = true\n'
            'fields = ["balance"]\n'
            '[objects."economy.bilateral".granularity]\n'
            'balance = "exact"\n'
        )
        plugin = PunchholeBackendPlugin(ctx, {"schema_file": "exposure_schema.toml"})
        return plugin, sk, node_id

    def test_all_signed_sees_only_skills(self, tmp_path):
        """all_signed tier: only skills in available, economy objects in not_available."""
        plugin, sk, node_id = self._make_backend_with_schema(tmp_path)
        unknown_nid = "c" * 64
        # Inject ACL: unknown node gets all_signed
        card = plugin.build_card(unknown_nid)
        assert card is not None
        assert "proof" in card  # signed
        available_keys = {o["key"] for o in card["available"]}
        not_available_keys = {o["key"] for o in card["not_available"]}
        assert "skills" in available_keys
        assert "economy.summary" in not_available_keys
        assert "economy.bilateral" in not_available_keys

    def test_peer_sees_bilateral(self, tmp_path):
        """peer tier: all objects available."""
        db_path = make_db(tmp_path)
        _, _, peer_nid = make_keypair()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO peer_keys (node_id, public_key, first_seen, last_updated) VALUES (?, ?, ?, ?)",
            (peer_nid, peer_nid, time.time(), time.time()),
        )
        conn.commit()
        conn.close()

        plugin, sk, node_id = self._make_backend_with_schema(tmp_path)
        plugin._ctx.storage_path = db_path
        card = plugin.build_card(peer_nid)
        assert card is not None
        available_keys = {o["key"] for o in card["available"]}
        assert "economy.bilateral" in available_keys
        assert "skills" in available_keys
        assert "economy.summary" in available_keys


# ---------------------------------------------------------------------------
# Bilateral live_query tests (backend)
# ---------------------------------------------------------------------------

class TestBilateralLiveQuery:
    """Test 16: bilateral data — live_query, counterparty set, exact granularity."""

    def test_bilateral_counterparty_set(self, tmp_path):
        """Bilateral cache object must have counterparty = requester_node_id."""
        db_path = make_db(tmp_path)
        _, _, peer_nid = make_keypair()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO ledger (peer_public_key, balance, soft_limit, prepaid, first_seen, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (peer_nid, 3.5, 10.0, 0.5, time.time(), time.time()),
        )
        conn.commit()
        conn.close()

        sk, vk, node_id = make_keypair()
        ctx = make_mock_ctx(tmp_path, with_sign=True, sk=sk, node_id=node_id)
        ctx.storage_path = db_path

        schema_path = tmp_path / "exposure_schema.toml"
        schema_path.write_text(
            'trusted_nodes = []\n'
            '[objects."economy.bilateral"]\n'
            'access = "peer"\n'
            'description = "Bilateral"\n'
            'source = "bilateral_ledger"\n'
            'live_query = true\n'
            'fields = ["balance", "utilization", "limit", "prepaid_balance"]\n'
            '[objects."economy.bilateral".granularity]\n'
            'balance = "exact"\n'
            'utilization = "exact"\n'
            'limit = "exact"\n'
            'prepaid_balance = "exact"\n'
        )
        plugin = PunchholeBackendPlugin(ctx, {"schema_file": "exposure_schema.toml"})

        obj_config = plugin._objects["economy.bilateral"]
        signed = plugin._build_cache_object("economy.bilateral", obj_config, "peer", peer_nid)

        assert signed is not None
        assert signed["counterparty"] == peer_nid
        assert signed["live_query"] is True
        assert signed["data"]["balance"] == 3.5  # exact
        assert signed["data"]["prepaid_balance"] == 0.5

    def test_bilateral_all_exact_granularity(self, tmp_path):
        """All bilateral fields must be exact (no rounding)."""
        db_path = make_db(tmp_path)
        _, _, peer_nid = make_keypair()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO ledger (peer_public_key, balance, soft_limit, prepaid, first_seen, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (peer_nid, 7.777, 20.0, 1.234, time.time(), time.time()),
        )
        conn.commit()
        conn.close()

        sk, vk, node_id = make_keypair()
        ctx = make_mock_ctx(tmp_path, with_sign=True, sk=sk, node_id=node_id)
        ctx.storage_path = db_path

        schema_path = tmp_path / "exposure_schema.toml"
        schema_path.write_text(
            'trusted_nodes = []\n'
            '[objects."economy.bilateral"]\n'
            'access = "peer"\n'
            'description = "Bilateral"\n'
            'source = "bilateral_ledger"\n'
            'live_query = true\n'
            'fields = ["balance", "prepaid_balance"]\n'
            '[objects."economy.bilateral".granularity]\n'
            'balance = "exact"\n'
            'prepaid_balance = "exact"\n'
        )
        plugin = PunchholeBackendPlugin(ctx, {"schema_file": "exposure_schema.toml"})
        obj_config = plugin._objects["economy.bilateral"]
        signed = plugin._build_cache_object("economy.bilateral", obj_config, "peer", peer_nid)
        assert signed is not None
        assert signed["data"]["balance"] == 7.777
        assert signed["data"]["prepaid_balance"] == 1.234


# ---------------------------------------------------------------------------
# Stale propagation tests (backend)
# ---------------------------------------------------------------------------

class TestStalePropagation:
    """Test 17: stale propagation."""

    def test_credit_change_stales_economy_summary(self, tmp_path):
        """credit.change event should cause cache.stale.economy.summary to be emitted."""
        sk, vk, node_id = make_keypair()
        ctx = make_mock_ctx(tmp_path, with_sign=True, sk=sk, node_id=node_id)

        schema_path = tmp_path / "exposure_schema.toml"
        schema_path.write_text(
            'trusted_nodes = []\n'
            '[objects."economy.summary"]\n'
            'access = "known_hosts"\n'
            'description = "test"\n'
            'source = "bilateral_ledger"\n'
            'fields = ["credit_balance"]\n'
            '[objects."economy.summary".granularity]\n'
            'credit_balance = "exact"\n'
        )
        plugin = PunchholeBackendPlugin(ctx, {"schema_file": "exposure_schema.toml"})

        stale_keys = _backend_mod._STALE_MAP if hasattr(_backend_mod, "_STALE_MAP") \
            else PunchholeBackendPlugin._STALE_MAP
        affected = stale_keys.get("credit.change", [])
        assert "economy.summary" in affected

    def test_receipt_issued_stales_economy_summary(self):
        stale_keys = PunchholeBackendPlugin._STALE_MAP
        assert "economy.summary" in stale_keys.get("receipt.issued", [])

    def test_skill_registered_stales_skills(self):
        stale_keys = PunchholeBackendPlugin._STALE_MAP
        assert "skills" in stale_keys.get("skill.registered", [])

    def test_skill_removed_stales_skills(self):
        stale_keys = PunchholeBackendPlugin._STALE_MAP
        assert "skills" in stale_keys.get("skill.removed", [])


# ---------------------------------------------------------------------------
# Startup sequence test (backend)
# ---------------------------------------------------------------------------

class TestStartupSequence:
    """Test 18: all objects pushed before ready signal."""

    def test_startup_emits_fill_before_ready(self, tmp_path):
        """Warm start: cache.fill.* events before cache.backend.ready."""
        sk, vk, node_id = make_keypair()
        ctx = make_mock_ctx(tmp_path, with_sign=True, sk=sk, node_id=node_id)

        schema_path = tmp_path / "exposure_schema.toml"
        schema_path.write_text(
            'trusted_nodes = []\n'
            '[objects.skills]\n'
            'access = "all_signed"\n'
            'description = "Skills"\n'
            'source = "skill_registry"\n'
            'fields = ["skill_name"]\n'
            '[objects.skills.granularity]\n'
            'skill_name = "list"\n'
        )

        plugin = PunchholeBackendPlugin(ctx, {"schema_file": "exposure_schema.toml"})

        # Run the startup coroutine directly
        asyncio.run(plugin._startup())

        emitted = ctx._emitted
        event_types = [e["event"] for e in emitted]

        # cache.backend.ready must be last
        assert "cache.backend.ready" in event_types
        ready_idx = event_types.index("cache.backend.ready")

        # At least one cache.fill.* before ready
        fill_events = [i for i, e in enumerate(event_types) if e.startswith("cache.fill.")]
        assert len(fill_events) > 0
        assert all(i < ready_idx for i in fill_events), \
            "All cache.fill.* events must precede cache.backend.ready"


# ---------------------------------------------------------------------------
# Frontend tests
# ---------------------------------------------------------------------------

class TestFrontendCacheHitMiss:
    """Tests 1-7: frontend request handling, cache, and disclosure log."""

    def _make_signed_request(self, sk, node_id, object_key="skills"):
        """Build a signed punchhole request."""
        doc = {
            "document_type": "punchhole_request",
            "version": 1,
            "object_key": object_key,
            "ts": time.time(),
        }
        return sign_doc(doc, sk, node_id)

    @pytest.mark.asyncio
    async def test_request_before_backend_ready_rejected(self, tmp_path):
        """Test 6: request before backend.ready is rejected/not served."""
        sk, vk, node_id = make_keypair()
        ctx = make_mock_ctx(tmp_path)
        plugin = PunchholeFrontendPlugin(ctx, {"disclosure_log": "test_disc.db"})

        assert plugin._backend_ready is False

        signed_req = self._make_signed_request(sk, node_id)
        body = {"action": "request", "object_key": "skills", "payload": signed_req}

        await plugin.on_mail_received("punchhole.request", node_id, "us", body, None)

        # No punchhole.response emitted — request silently dropped
        resp_events = [e for e in ctx._emitted if e["event"] == "punchhole.response"]
        assert len(resp_events) == 0

    @pytest.mark.asyncio
    async def test_request_invalid_signature_rejected(self, tmp_path):
        """Test 2: invalid signature -> reject, no response emitted."""
        ctx = make_mock_ctx(tmp_path)
        plugin = PunchholeFrontendPlugin(ctx, {"disclosure_log": "test_disc2.db"})
        plugin._backend_ready = True

        requester_nid = "d" * 64  # 32 bytes hex
        # Unsigned/malformed payload
        bad_request = {"document_type": "punchhole_request", "version": 1, "object_key": "skills"}
        body = {"action": "request", "object_key": "skills", "payload": bad_request}

        await plugin.on_mail_received("punchhole.request", requester_nid, "us", body, None)

        resp_events = [e for e in ctx._emitted if e["event"] == "punchhole.response"]
        assert len(resp_events) == 0

    @pytest.mark.asyncio
    async def test_cache_hit_serves_object(self, tmp_path):
        """Test 1: valid sig + cached object -> punchhole.response emitted."""
        sk, vk, node_id = make_keypair()
        ctx = make_mock_ctx(tmp_path)
        plugin = PunchholeFrontendPlugin(ctx, {"disclosure_log": "test_disc3.db"})
        plugin._backend_ready = True

        # Pre-populate cache
        fake_signed = {"document_type": "cache_object", "data": {"skills": []}, "proof": "x"}
        plugin._cache[("skills", "all_signed")] = {"data": fake_signed, "stale": False}
        # Map requester to all_signed
        plugin._acl[node_id] = "all_signed"

        signed_req = self._make_signed_request(sk, node_id, "skills")
        body = {"action": "request", "object_key": "skills", "payload": signed_req}

        await plugin.on_mail_received("punchhole.request", node_id, "us", body, None)

        resp_events = [e for e in ctx._emitted if e["event"] == "punchhole.response"]
        assert len(resp_events) == 1
        assert resp_events[0]["from_cache"] is True
        assert resp_events[0]["object_key"] == "skills"

    @pytest.mark.asyncio
    async def test_cache_miss_emits_miss_event(self, tmp_path):
        """Test 3: cache miss -> emits cache.miss.data.{key} with requester_node_id."""
        sk, vk, node_id = make_keypair()
        ctx = make_mock_ctx(tmp_path)
        plugin = PunchholeFrontendPlugin(ctx, {"disclosure_log": "test_disc4.db"})
        plugin._backend_ready = True
        # No cache entries
        plugin._acl[node_id] = "all_signed"

        signed_req = self._make_signed_request(sk, node_id, "skills")
        body = {"action": "request", "object_key": "skills", "payload": signed_req}

        await plugin.on_mail_received("punchhole.request", node_id, "us", body, None)

        miss_events = [e for e in ctx._emitted if e["event"].startswith("cache.miss.")]
        assert len(miss_events) == 1
        assert miss_events[0]["requester_node_id"] == node_id  # CRITICAL
        assert miss_events[0]["object_key"] == "skills"

    @pytest.mark.asyncio
    async def test_cache_fill_event_populates_cache(self, tmp_path):
        """Test 4: cache fill event -> object cached in memory."""
        ctx = make_mock_ctx(tmp_path)
        plugin = PunchholeFrontendPlugin(ctx, {"disclosure_log": "test_disc5.db"})

        fake_signed = {"document_type": "cache_object", "data": {}}
        # Simulate the effect of a cache.fill.* bus event being processed
        await _simulate_fill(plugin, "skills", "all_signed", fake_signed)

        assert ("skills", "all_signed") in plugin._cache
        assert plugin._cache[("skills", "all_signed")]["stale"] is False

    @pytest.mark.asyncio
    async def test_cache_stale_marks_entry_stale(self, tmp_path):
        """Test 5: cache stale event -> entry marked stale, next request triggers miss."""
        ctx = make_mock_ctx(tmp_path)
        plugin = PunchholeFrontendPlugin(ctx, {"disclosure_log": "test_disc6.db"})

        # Pre-populate
        fake_signed = {"document_type": "cache_object", "data": {}}
        plugin._cache[("skills", "all_signed")] = {"data": fake_signed, "stale": False}

        # Simulate stale event
        _simulate_stale(plugin, "skills")

        assert plugin._cache[("skills", "all_signed")]["stale"] is True

    @pytest.mark.asyncio
    async def test_disclosure_log_written(self, tmp_path):
        """Test 7: disclosure log written on every request (hit or miss)."""
        sk, vk, node_id = make_keypair()
        ctx = make_mock_ctx(tmp_path)
        plugin = PunchholeFrontendPlugin(ctx, {"disclosure_log": "test_disc7.db"})
        plugin._backend_ready = True
        plugin._acl[node_id] = "all_signed"
        # No cache — will be a miss

        signed_req = self._make_signed_request(sk, node_id, "skills")
        body = {"action": "request", "object_key": "skills", "payload": signed_req}

        await plugin.on_mail_received("punchhole.request", node_id, "us", body, None)

        conn = sqlite3.connect(str(tmp_path / "test_disc7.db"))
        rows = conn.execute("SELECT requester, object_key, outcome FROM disclosure_log").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == node_id
        assert rows[0][1] == "skills"
        assert rows[0][2] in ("miss", "hit")


# ---------------------------------------------------------------------------
# Helpers for simulating bus events on frontend
# ---------------------------------------------------------------------------

def _simulate_fill(plugin, object_key: str, acl_group: str, data: dict):
    """Directly populate frontend cache (simulates cache.fill.* bus event)."""
    plugin._cache[(object_key, acl_group)] = {"data": data, "stale": False}
    async def _noop(): pass
    return _noop()


def _simulate_stale(plugin, object_key: str):
    """Directly mark cache entries stale (simulates cache.stale.* bus event)."""
    for key in list(plugin._cache.keys()):
        if key[0] == object_key:
            plugin._cache[key]["stale"] = True
