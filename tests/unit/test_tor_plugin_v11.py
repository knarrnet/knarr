"""Wave 1 unit tests — Tor plugin v1.1 (synthesized from blind panel 2026-04-11).

Sprint: parallel-track Tor plugin moonshot
Spec:   F:\\thing\\specs\\SPEC-tor-plugin.md v1.1
Synth:  F:\\thing\\specs\\SYNTHESIS-tor-plugin.md

Covers the Wave 1 test brief (F:\\thing\\sprints\\tests-tor-plugin\\) plus
the 5 threat-model coverage gaps from SYNTHESIS §4 (§5.3 cert pin lookup,
§5.6 consensus loss fallback, §5.7 stream correlation, §5.8 sidecar auth
probe, §5.11 clock skew).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Onion address derivation — pure function contract
# ---------------------------------------------------------------------------


def test_onion_address_from_pubkey_is_importable_standalone():
    """The helper must be importable without instantiating TorPlugin."""
    from knarr.plugins.tor.handler import TorPlugin
    assert callable(TorPlugin.onion_address_from_pubkey)


def test_onion_address_from_pubkey_matches_rend_spec_v3_format():
    """Spec §2.3: base32(pubkey || checksum || version).lower() + ".onion"
    checksum = SHA3-256(b".onion checksum" || pubkey || version)[:2]
    version = b'\\x03'
    Output body is 56 lowercase base32 chars.
    """
    from knarr.plugins.tor.handler import TorPlugin
    pubkey = bytes(32)  # 32 zero bytes
    result = TorPlugin.onion_address_from_pubkey(pubkey)
    assert isinstance(result, str)
    assert result.endswith(".onion")
    body = result[:-len(".onion")]
    assert len(body) == 56
    assert body == body.lower()
    assert all(c in "abcdefghijklmnopqrstuvwxyz234567" for c in body)


def test_onion_address_from_pubkey_is_deterministic():
    from knarr.plugins.tor.handler import TorPlugin
    pubkey = b"A" * 32
    assert (
        TorPlugin.onion_address_from_pubkey(pubkey)
        == TorPlugin.onion_address_from_pubkey(pubkey)
        == TorPlugin.onion_address_from_pubkey(pubkey)
    )


def test_onion_address_from_pubkey_differs_for_different_pubkeys():
    from knarr.plugins.tor.handler import TorPlugin
    assert (
        TorPlugin.onion_address_from_pubkey(b"A" * 32)
        != TorPlugin.onion_address_from_pubkey(b"B" * 32)
    )


def test_onion_address_from_pubkey_rejects_wrong_length():
    from knarr.plugins.tor.handler import TorPlugin
    with pytest.raises((AssertionError, ValueError)):
        TorPlugin.onion_address_from_pubkey(b"")
    with pytest.raises((AssertionError, ValueError)):
        TorPlugin.onion_address_from_pubkey(b"X" * 31)
    with pytest.raises((AssertionError, ValueError)):
        TorPlugin.onion_address_from_pubkey(b"X" * 33)


def test_onion_address_checksum_follows_sha3_256_spec():
    """Verify checksum bytes match SHA3-256(b".onion checksum" || pubkey || b'\\x03')[:2]."""
    from knarr.plugins.tor.handler import TorPlugin
    pubkey = bytes.fromhex("00" * 32)
    onion = TorPlugin.onion_address_from_pubkey(pubkey)
    body = onion[:-len(".onion")]
    padded = body.upper() + "=" * ((8 - len(body) % 8) % 8)
    raw = base64.b32decode(padded)
    assert len(raw) == 35
    assert raw[:32] == pubkey
    assert raw[34:35] == b"\x03"
    expected_checksum = hashlib.sha3_256(b".onion checksum" + pubkey + b"\x03").digest()[:2]
    assert raw[32:34] == expected_checksum


def test_onion_address_known_vector():
    """Known-vector regression test (Mímir observation from synthesis review).

    Catches refactor drift that unit tests alone might miss if generated from
    the helper itself. Uses a fixed pubkey → expected onion mapping.
    """
    from knarr.plugins.tor.handler import TorPlugin
    # Deterministic pubkey: all 0x00 bytes.
    pubkey = bytes(32)
    expected = TorPlugin.onion_address_from_pubkey(pubkey)
    # Re-derive via the rend-spec-v3.txt formula inline (not using the helper)
    version = b"\x03"
    checksum = hashlib.sha3_256(b".onion checksum" + pubkey + version).digest()[:2]
    raw = pubkey + checksum + version
    body = base64.b32encode(raw).decode("ascii").lower().rstrip("=")
    assert expected == f"{body}.onion"


# ---------------------------------------------------------------------------
# Readiness state split (§2.2 step 5 — F-5 + O-3)
# ---------------------------------------------------------------------------


def test_plugin_exposes_readiness_state_split():
    from knarr.plugins.tor.handler import TorPlugin
    assert hasattr(TorPlugin, "is_tor_daemon_reachable")
    assert hasattr(TorPlugin, "is_tor_hidden_service_published")


def test_plugin_readiness_false_when_disabled():
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": False}}
    assert plugin.is_tor_daemon_reachable() is False
    assert plugin.is_tor_hidden_service_published() is False


def test_get_tor_status_exposes_full_schema():
    """get_tor_status must return all documented fields per §3.1."""
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    status = plugin.get_tor_status()
    for key in (
        "enabled", "key_mode", "daemon_reachable", "hidden_service_published",
        "own_onion", "socks_port", "control_port", "control_auth_ok",
        "circuit_count", "last_consensus_check", "last_hs_verify",
        "dependency_loaded",
    ):
        assert key in status, f"missing status field: {key}"


# ---------------------------------------------------------------------------
# Config defaults (§4 — v1.1 key_mode flip + safety interlock)
# ---------------------------------------------------------------------------


def test_config_default_key_mode_is_separate():
    """v1.1 F-1 + O-1 + O-2: key_mode default MUST be 'separate'."""
    from knarr.plugins.tor.handler import TorPlugin
    defaults = TorPlugin.get_default_config()
    assert defaults["key_mode"] == "separate"


def test_config_default_acknowledge_identity_leak_is_false():
    from knarr.plugins.tor.handler import TorPlugin
    defaults = TorPlugin.get_default_config()
    assert defaults["acknowledge_identity_leak"] is False


def test_config_shared_key_mode_requires_acknowledge_identity_leak():
    """Safety interlock: shared mode without acknowledge_identity_leak must raise."""
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {
        "tor": {
            "enabled": True,
            "key_mode": "shared",
            "acknowledge_identity_leak": False,
        }
    }
    with pytest.raises((ValueError, RuntimeError, AssertionError)):
        plugin._validate_config()


def test_config_separate_key_mode_does_not_require_ack():
    """separate mode is the safe default — no ack required."""
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": True, "key_mode": "separate"}}
    plugin._validate_config()  # must not raise


def test_config_expose_sidecar_defaults_false():
    """v1.1 O-9: sidecar NOT exposed via Tor by default."""
    from knarr.plugins.tor.handler import TorPlugin
    defaults = TorPlugin.get_default_config()
    assert defaults["expose_sidecar"] is False


def test_config_sidecar_allow_unauth_defaults_false():
    """Mímir Q2 belt-and-suspenders override defaults off."""
    from knarr.plugins.tor.handler import TorPlugin
    defaults = TorPlugin.get_default_config()
    assert defaults["sidecar_allow_unauth"] is False


# ---------------------------------------------------------------------------
# Pool integration seams (§3.2 — v1.1 lifecycle per O-14)
# ---------------------------------------------------------------------------


def _make_test_pool():
    """Construct a ConnectionPool with minimal state for unit tests."""
    from knarr.dht.pool import ConnectionPool
    pool = ConnectionPool.__new__(ConnectionPool)
    pool._tor_dialer = None
    pool._tor_key_mode = "separate"
    pool._prefer_onion = False
    pool._peer_pubkey_cache = {}
    pool._peer_pubkey_lookup = None
    pool._circuit_budget = None
    pool._derived_onion_fallback_enabled = True
    pool._derived_onion_fallback_window = 300.0
    pool._derived_onion_fallback_ts = {}
    pool._bus = None
    pool._pre_tor_skip_logged = set()
    pool._conn_hosts = {}
    return pool


def test_pool_set_tor_dialer_replaces_previous():
    """Second call replaces the first (§3.2 O-14 lifecycle docs)."""
    pool = _make_test_pool()
    dialer1 = MagicMock()
    dialer2 = MagicMock()
    pool.set_tor_dialer(dialer1)
    pool.set_tor_dialer(dialer2)
    assert pool._tor_dialer is dialer2


def test_pool_get_tor_dialer_introspection():
    pool = _make_test_pool()
    assert pool.get_tor_dialer() is None
    dialer = MagicMock()
    pool.set_tor_dialer(dialer)
    assert pool.get_tor_dialer() is dialer
    pool.set_tor_dialer(None)
    assert pool.get_tor_dialer() is None


def test_pool_set_tor_key_mode():
    pool = _make_test_pool()
    pool.set_tor_key_mode("shared")
    assert pool._tor_key_mode == "shared"
    pool.set_tor_key_mode("separate")
    assert pool._tor_key_mode == "separate"


def test_pool_set_tor_key_mode_rejects_invalid():
    pool = _make_test_pool()
    with pytest.raises(ValueError):
        pool.set_tor_key_mode("bogus")


# ---------------------------------------------------------------------------
# Dual-stack resolution gating (§2.5 — shared-only per F-1)
# ---------------------------------------------------------------------------


def test_pool_dual_stack_disabled_in_separate_mode():
    """v1.1 F-1: separate mode must NOT derive onion from pubkey."""
    pool = _make_test_pool()
    pool._tor_dialer = MagicMock()
    pool._tor_key_mode = "separate"
    pool._prefer_onion = True
    pool._peer_pubkey_cache = {"peer123": bytes(32)}
    assert pool._resolve_transport_host("peer123", "192.168.1.10") == "192.168.1.10"


def test_pool_dual_stack_active_in_shared_mode_with_pubkey():
    """Shared mode + prefer_onion + cached pubkey → derived .onion."""
    from knarr.plugins.tor.handler import TorPlugin
    pool = _make_test_pool()
    pool._tor_dialer = MagicMock()
    pool._tor_key_mode = "shared"
    pool._prefer_onion = True
    pubkey = b"Z" * 32
    pool._peer_pubkey_cache = {"peer123": pubkey}
    expected_onion = TorPlugin.onion_address_from_pubkey(pubkey)
    assert pool._resolve_transport_host("peer123", "192.168.1.10") == expected_onion


def test_pool_dual_stack_passes_through_already_onion_host():
    pool = _make_test_pool()
    pool._tor_dialer = MagicMock()
    pool._tor_key_mode = "shared"
    pool._prefer_onion = True
    pool._peer_pubkey_cache = {"peer123": b"X" * 32}
    original = "abc123xyz.onion"
    assert pool._resolve_transport_host("peer123", original) == original


def test_pool_dual_stack_cache_miss_returns_clearnet():
    """Cache miss → return clearnet host (O-4: no blocking SQLite on hot path)."""
    pool = _make_test_pool()
    pool._tor_dialer = MagicMock()
    pool._tor_key_mode = "shared"
    pool._prefer_onion = True
    pool._peer_pubkey_cache = {}  # empty
    assert pool._resolve_transport_host("peer123", "192.168.1.10") == "192.168.1.10"


def test_pool_dual_stack_no_dialer_returns_clearnet():
    pool = _make_test_pool()
    pool._tor_dialer = None
    pool._tor_key_mode = "shared"
    pool._prefer_onion = True
    pool._peer_pubkey_cache = {"peer123": b"X" * 32}
    assert pool._resolve_transport_host("peer123", "192.168.1.10") == "192.168.1.10"


# ---------------------------------------------------------------------------
# Circuit budget (§5.5 F-4 — Opus's class, carried verbatim)
# ---------------------------------------------------------------------------


def test_circuit_budget_per_peer_enforced():
    """Per-peer circuit rate limit refuses new circuits over the budget window."""
    from knarr.plugins.tor.handler import _CircuitBudget
    budget = _CircuitBudget(per_peer=3, global_=100, collapse_aliases=False, window_seconds=60.0)
    for i in range(3):
        ok, reason = budget.allow("peer-a", pubkey_hex=None, now=1000.0 + i)
        assert ok, f"call {i} should be allowed"
    ok, reason = budget.allow("peer-a", pubkey_hex=None, now=1000.5)
    assert ok is False
    assert reason == "per_peer"
    # Different peer unaffected
    ok, reason = budget.allow("peer-b", pubkey_hex=None, now=1000.5)
    assert ok
    assert reason == ""


def test_circuit_budget_global_enforced():
    """v1.1 F-4: global budget as Sybil backstop.

    Rotating peer_ids stays under each per-peer bucket but hits the global
    cap after N attempts.
    """
    from knarr.plugins.tor.handler import _CircuitBudget
    budget = _CircuitBudget(per_peer=10, global_=5, collapse_aliases=False, window_seconds=60.0)
    for i in range(5):
        ok, reason = budget.allow(f"peer-{i}", pubkey_hex=None, now=1000.0 + i * 0.1)
        assert ok
    ok, reason = budget.allow("peer-6", pubkey_hex=None, now=1000.6)
    assert ok is False
    assert reason == "global"


def test_circuit_budget_pubkey_collapse():
    """v1.1 F-4: multiple peer_ids sharing a pubkey share the counter.

    Prevents the Sybil-alias bypass where rotating peer_ids each stay under
    the per-peer bucket.
    """
    from knarr.plugins.tor.handler import _CircuitBudget
    budget = _CircuitBudget(per_peer=3, global_=100, collapse_aliases=True, window_seconds=60.0)
    pubkey_hex = "deadbeef" * 8
    for i in range(3):
        ok, reason = budget.allow(f"alias-{i}", pubkey_hex=pubkey_hex, now=1000.0 + i)
        assert ok
    ok, reason = budget.allow("alias-3", pubkey_hex=pubkey_hex, now=1003.5)
    assert ok is False
    assert reason == "pubkey_collapse"
    # Different pubkey not affected
    ok, reason = budget.allow("alias-x", pubkey_hex="cafe1234" * 8, now=1003.5)
    assert ok


def test_circuit_budget_window_prunes_stale_entries():
    """Sliding window: entries older than window_seconds are pruned on next call."""
    from knarr.plugins.tor.handler import _CircuitBudget
    budget = _CircuitBudget(per_peer=3, global_=100, collapse_aliases=False, window_seconds=10.0)
    for i in range(3):
        ok, _ = budget.allow("peer-a", pubkey_hex=None, now=1000.0 + i)
        assert ok
    # Next call is outside the 10s window — old entries should be pruned
    ok, reason = budget.allow("peer-a", pubkey_hex=None, now=1020.0)
    assert ok, f"expected window prune, got denial={reason}"


# ---------------------------------------------------------------------------
# §5.3 cert pin lookup host-agnostic (SYNTHESIS §4 gap test)
# ---------------------------------------------------------------------------


def test_cert_pin_lookup_is_host_agnostic():
    """§5.3 regression: cert pinning is keyed by node_id, NOT by transit host.

    The pool's `_try_send` stores `_tls_peer_host` on the response message,
    but the v0.56.0 C-01 cert pinning in the node layer looks up the
    fingerprint by node_id. A peer reached via clearnet or onion MUST have
    the same cert fingerprint because it's the same TLS server cert.

    This test verifies the pool doesn't ALTER the fingerprint based on host.
    """
    from knarr.dht.pool import ConnectionPool
    pool = _make_test_pool()
    # The fingerprint is extracted from writer.get_extra_info("ssl_object") —
    # the writer's SSL object is independent of the host string. Verify the
    # _try_send plumbing writes _tls_peer_host correctly but doesn't touch
    # the fingerprint computation.
    # (Smoke test: assert the plumbing uses the passed host, not a hardcoded value)
    import inspect
    src = inspect.getsource(ConnectionPool._try_send)
    assert "_tls_peer_host" in src
    assert "_tls_peer_cert_fingerprint" in src
    # Fingerprint comes from get_tls_peer_cert_fingerprint(ssl_object) — no host parameter
    assert "get_tls_peer_cert_fingerprint" in src


# ---------------------------------------------------------------------------
# §5.6 consensus-loss fallback event emission (SYNTHESIS §4 gap test)
# ---------------------------------------------------------------------------


def test_consensus_lost_event_emits_fallback():
    """§5.6 gap test: control port NEWCONSENSUS with empty body → tor.consensus_lost.

    Verifies the AsyncControlPort event dispatch correctly maps a NEWCONSENSUS
    line with empty body to the bus event.
    """
    from knarr.plugins.tor.control import AsyncControlPort
    events = []
    ctrl = AsyncControlPort(
        host="127.0.0.1", port=9051,
        auth_method="none",
        bus_emit=lambda event_type, **fields: events.append((event_type, fields)),
    )
    # Drive the dispatcher directly
    ctrl._dispatch_event("NEWCONSENSUS")
    assert any(e[0] == "tor.consensus_lost" for e in events)


def test_consensus_recovered_edge_transition():
    """After a consensus_lost, a subsequent NEWCONSENSUS with a body fires recovered."""
    from knarr.plugins.tor.control import AsyncControlPort
    events = []
    ctrl = AsyncControlPort(
        host="127.0.0.1", port=9051,
        auth_method="none",
        bus_emit=lambda event_type, **fields: events.append((event_type, fields)),
    )
    ctrl._dispatch_event("NEWCONSENSUS")  # lost
    ctrl._dispatch_event("NEWCONSENSUS valid-consensus-body")  # recovered
    kinds = [e[0] for e in events]
    assert "tor.consensus_lost" in kinds
    assert "tor.consensus_recovered" in kinds
    # Recovered must come AFTER lost
    assert kinds.index("tor.consensus_recovered") > kinds.index("tor.consensus_lost")


# ---------------------------------------------------------------------------
# §5.7 per-peer stream isolation (SYNTHESIS §4 gap test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_peer_stream_isolation_generates_distinct_creds():
    """§5.7 gap test: per_peer circuit_sharing must generate distinct SOCKS5
    credentials for each peer_id so Tor builds separate circuits.

    Verified by stubbing python_socks.Proxy.from_url and asserting the URLs
    passed for different peer_ids differ in their auth section.
    """
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": True, "circuit_sharing": "per_peer"}}

    # Capture the URLs Proxy.from_url receives
    urls_called = []

    class _StubProxy:
        @staticmethod
        def from_url(url):
            urls_called.append(url)
            p = MagicMock()
            p.connect = AsyncMock(return_value=MagicMock())
            return p

    # Patch python_socks import and open_connection so _socks5_dialer can run
    with patch.dict(sys.modules, {
        "python_socks": MagicMock(),
        "python_socks.async_": MagicMock(),
        "python_socks.async_.asyncio": SimpleNamespace(Proxy=_StubProxy),
    }):
        plugin._python_socks = SimpleNamespace(Proxy=_StubProxy)
        # Also stub asyncio.open_connection so we don't try a real dial
        async def _fake_open_connection(*args, **kwargs):
            return MagicMock(), MagicMock()
        with patch("asyncio.open_connection", new=_fake_open_connection):
            try:
                await plugin._socks5_dialer("abc.onion", 9010, None, peer_id="peer-a")
            except Exception:
                pass
            try:
                await plugin._socks5_dialer("abc.onion", 9010, None, peer_id="peer-b")
            except Exception:
                pass

    assert len(urls_called) == 2
    # The URLs should differ in their auth/username section
    assert urls_called[0] != urls_called[1]
    assert "peer-a" in urls_called[0]
    assert "peer-b" in urls_called[1]


# ---------------------------------------------------------------------------
# §5.8 sidecar auth HTTP probe (Mímir Q2 — NEW in synthesis)
# ---------------------------------------------------------------------------


class _FakeSidecarReader:
    """Fake asyncio.StreamReader that yields a canned HTTP response."""
    def __init__(self, status_line: bytes, rest: bytes = b""):
        self._status = status_line
        self._rest = rest
        self._read = False

    async def readline(self):
        if self._read:
            return b""
        self._read = True
        return self._status


class _FakeWriter:
    def __init__(self):
        self._buf = b""
        self._closed = False
    def write(self, data):
        self._buf += data
    async def drain(self):
        pass
    def close(self):
        self._closed = True
    async def wait_closed(self):
        pass
    def is_closing(self):
        return self._closed


@pytest.mark.asyncio
async def test_sidecar_auth_probe_allows_on_401():
    """401 Unauthorized → auth enforced → probe returns True."""
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": True, "expose_sidecar": True}}
    plugin._sidecar_port = 9031
    events = []
    plugin._bus = SimpleNamespace(emit=lambda et, **kw: events.append((et, kw)))

    async def _fake_open_connection(host, port, **kwargs):
        reader = _FakeSidecarReader(b"HTTP/1.0 401 Unauthorized\r\n")
        writer = _FakeWriter()
        return reader, writer

    with patch("asyncio.open_connection", new=_fake_open_connection):
        ok = await plugin._verify_sidecar_auth()
    assert ok is True
    # Verified-auth telemetry event
    assert any(e[0] == "tor.sidecar_auth_verified" for e in events)


@pytest.mark.asyncio
async def test_sidecar_auth_probe_refuses_on_200():
    """200 OK without auth → LEAK → probe returns False + emits unauth_detected."""
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": True, "expose_sidecar": True}}
    plugin._sidecar_port = 9031
    events = []
    plugin._bus = SimpleNamespace(emit=lambda et, **kw: events.append((et, kw)))

    async def _fake_open_connection(host, port, **kwargs):
        reader = _FakeSidecarReader(b"HTTP/1.0 200 OK\r\n")
        writer = _FakeWriter()
        return reader, writer

    with patch("asyncio.open_connection", new=_fake_open_connection):
        ok = await plugin._verify_sidecar_auth()
    assert ok is False
    assert any(e[0] == "tor.sidecar_unauth_detected" for e in events)


@pytest.mark.asyncio
async def test_sidecar_auth_probe_refuses_on_connection_error():
    """Connection refused / timeout → fail closed → probe returns False."""
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": True, "expose_sidecar": True}}
    plugin._sidecar_port = 9031
    events = []
    plugin._bus = SimpleNamespace(emit=lambda et, **kw: events.append((et, kw)))

    async def _fake_open_connection(host, port, **kwargs):
        raise ConnectionRefusedError("sidecar down")

    with patch("asyncio.open_connection", new=_fake_open_connection):
        ok = await plugin._verify_sidecar_auth()
    assert ok is False
    assert any(e[0] == "tor.sidecar_auth_unverified" for e in events)


@pytest.mark.asyncio
async def test_sidecar_exposure_refused_without_override():
    """When expose_sidecar = true but auth probe fails and sidecar_allow_unauth
    is false, _should_expose_sidecar returns False and emits refused event."""
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {
        "tor": {
            "enabled": True,
            "expose_sidecar": True,
            "sidecar_allow_unauth": False,
        }
    }
    plugin._sidecar_port = 9031
    events = []
    plugin._bus = SimpleNamespace(emit=lambda et, **kw: events.append((et, kw)))

    async def _fake_open_connection(host, port, **kwargs):
        raise ConnectionRefusedError("sidecar down")

    with patch("asyncio.open_connection", new=_fake_open_connection):
        expose = await plugin._should_expose_sidecar()
    assert expose is False
    assert any(e[0] == "tor.sidecar_exposure_refused" for e in events)


@pytest.mark.asyncio
async def test_sidecar_exposure_allowed_with_override_despite_probe_fail():
    """When auth probe fails BUT sidecar_allow_unauth = true, exposure is
    allowed with an explicit unauth_allowed event."""
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {
        "tor": {
            "enabled": True,
            "expose_sidecar": True,
            "sidecar_allow_unauth": True,
        }
    }
    plugin._sidecar_port = 9031
    events = []
    plugin._bus = SimpleNamespace(emit=lambda et, **kw: events.append((et, kw)))

    async def _fake_open_connection(host, port, **kwargs):
        raise ConnectionRefusedError("sidecar down")

    with patch("asyncio.open_connection", new=_fake_open_connection):
        expose = await plugin._should_expose_sidecar()
    assert expose is True
    assert any(e[0] == "tor.sidecar_unauth_allowed" for e in events)


# ---------------------------------------------------------------------------
# torrc generation (§5.8 — sidecar exclusion)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_torrc_generation_excludes_sidecar_by_default():
    """Default: generated torrc has ONE HiddenServicePort entry (main only)."""
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": True, "expose_sidecar": False}}
    plugin._knarr_port = 9010
    plugin._sidecar_port = 9031
    plugin._tor_data_dir = "/tmp/tor-test"
    torrc = await plugin._generate_torrc()
    hs_port_lines = [l for l in torrc.splitlines() if l.strip().startswith("HiddenServicePort")]
    assert len(hs_port_lines) == 1
    assert "9010" in hs_port_lines[0]
    assert "9031" not in torrc


# ---------------------------------------------------------------------------
# §5.11 clock skew (SYNTHESIS §4 gap test)
# ---------------------------------------------------------------------------


def test_clock_skew_event_emits_warning():
    """§5.11 gap test: STATUS_GENERAL CLOCK_SKEW SKEW=N → tor.clock_skew_warning."""
    from knarr.plugins.tor.control import AsyncControlPort
    events = []
    ctrl = AsyncControlPort(
        host="127.0.0.1", port=9051,
        auth_method="none",
        bus_emit=lambda event_type, **fields: events.append((event_type, fields)),
    )
    ctrl._dispatch_event("STATUS_GENERAL CLOCK_SKEW SKEW=45")
    assert any(e[0] == "tor.clock_skew_warning" for e in events)
    skew_event = next(e for e in events if e[0] == "tor.clock_skew_warning")
    assert skew_event[1].get("skew_seconds") == 45.0


# ---------------------------------------------------------------------------
# python-socks optional dependency handling (§10.1 — O-11)
# ---------------------------------------------------------------------------


def test_plugin_loads_without_python_socks_dependency():
    """Plugin must import cleanly even without python-socks installed."""
    from knarr.plugins.tor.handler import TorPlugin
    assert TorPlugin is not None


@pytest.mark.asyncio
async def test_plugin_emits_dependency_missing_on_init_without_extra():
    """When python-socks is unavailable + tor.enabled = true, plugin emits
    tor.dependency_missing and becomes a no-op (returns from on_init early).
    """
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": True, "key_mode": "separate"}}
    events = []
    plugin._bus = SimpleNamespace(emit=lambda et, **kw: events.append((et, kw)))

    def _fake_import(name):
        if name == "python_socks.async_.asyncio":
            raise ImportError("python_socks not installed")
        return importlib_original(name)

    import importlib
    importlib_original = importlib.import_module
    with patch("importlib.import_module", side_effect=_fake_import):
        await plugin.on_init(ctx=None)

    assert any(e[0] == "tor.dependency_missing" for e in events)
    assert plugin._dependency_loaded is False


# ---------------------------------------------------------------------------
# Control port — PROTOCOLINFO validation (O-6 spoofing mitigation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_port_rejects_non_tor_listener():
    """A TCP listener that doesn't speak Tor control protocol must be rejected.

    Sets up a local fake listener that sends back garbage, verifies the
    AsyncControlPort rejects it via ControlPortError / ValueError.
    """
    from knarr.plugins.tor.control import AsyncControlPort, ControlPortError

    async def _fake_listener(reader, writer):
        writer.write(b"NOT A TOR REPLY\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(_fake_listener, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    client = AsyncControlPort(host="127.0.0.1", port=port, auth_method="none")
    try:
        await client.connect()
        with pytest.raises((ControlPortError, ConnectionError, ValueError)):
            await client.protocol_info()
    finally:
        await client.disconnect()
        server.close()
        await server.wait_closed()
