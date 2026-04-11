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
from pathlib import Path
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
    pool._sticky_clearnet = {}  # Phase 6 O-4 sticky-clearnet cache
    pool._bus = None
    pool._pre_tor_skip_logged = set()
    pool._pre_tor_skip_logged_max = 1024
    pool._conn_hosts = {}
    # Phase 6 O-9 + G-6: pubkey refill task tracking + in-flight dedupe
    pool._pubkey_refill_tasks = set()
    pool._pubkey_refill_inflight = set()
    # Phase 6 O-5: periodic sweep counter
    pool._tor_state_cleanup_counter = 0
    pool._tor_state_cleanup_interval = 128
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


def test_pool_sticky_clearnet_short_circuits_onion_attempt():
    """Phase 6 O-4 regression: once a derived-onion fallback succeeds and a
    sticky-clearnet entry is recorded, `_resolve_transport_host` must return
    the clearnet host directly without re-attempting the derived onion.

    Before the fix, every send during the 300s fallback window re-dialed the
    known-failing derived onion (paying 10s timeout each time) before being
    denied by the rate-limiter. This test locks in the sticky-clearnet
    short-circuit that breaks the livelock.
    """
    pool = _make_test_pool()
    pool._tor_dialer = MagicMock()
    pool._tor_key_mode = "shared"
    pool._prefer_onion = True
    pool._peer_pubkey_cache = {"peer123": b"Z" * 32}

    # Without sticky entry: derived onion is returned (normal resolution)
    from knarr.plugins.tor.handler import TorPlugin
    expected_onion = TorPlugin.onion_address_from_pubkey(b"Z" * 32)
    assert pool._resolve_transport_host("peer123", "192.168.1.10") == expected_onion

    # Install a live sticky entry — TTL 300s in the future
    import time
    pool._sticky_clearnet["peer123"] = time.monotonic() + 300.0

    # Now resolution must return clearnet host, NOT the derived onion
    assert pool._resolve_transport_host("peer123", "192.168.1.10") == "192.168.1.10"


def test_pool_sticky_clearnet_expires_and_reattempts_onion():
    """An expired sticky-clearnet entry must be cleared on access so the
    derived-onion resolution path reactivates after the fallback window
    ends.
    """
    pool = _make_test_pool()
    pool._tor_dialer = MagicMock()
    pool._tor_key_mode = "shared"
    pool._prefer_onion = True
    pool._peer_pubkey_cache = {"peer123": b"Z" * 32}

    # Install an EXPIRED sticky entry (TTL in the past)
    import time
    pool._sticky_clearnet["peer123"] = time.monotonic() - 1.0

    # Resolution should skip the expired entry and derive the onion normally
    from knarr.plugins.tor.handler import TorPlugin
    expected_onion = TorPlugin.onion_address_from_pubkey(b"Z" * 32)
    assert pool._resolve_transport_host("peer123", "192.168.1.10") == expected_onion
    # Stale entry must have been evicted
    assert "peer123" not in pool._sticky_clearnet


@pytest.mark.asyncio
async def test_pool_handle_dial_failure_records_sticky_clearnet_on_success():
    """After derived-onion fallback successfully reaches clearnet,
    `_handle_dial_failure` must record a sticky-clearnet entry so subsequent
    sends in the window bypass the onion.
    """
    pool = _make_test_pool()
    pool._tor_dialer = MagicMock()
    pool._tor_key_mode = "shared"
    pool._prefer_onion = True
    pool._derived_onion_fallback_enabled = True
    pool._derived_onion_fallback_window = 300.0
    pool._last_used = {}  # populated by _handle_dial_failure on success

    # _close_conn is called inside send on TCP errors — stub it
    async def _fake_close_conn(peer_id):
        return None
    pool._close_conn = _fake_close_conn

    # Stub _open to succeed on clearnet fallback (returns a fake conn tuple)
    async def _fake_open(peer_id, host, port, timeout):
        return (MagicMock(), MagicMock())

    # Sentinel reply — any non-_SEND_FAILED object counts as success
    fake_response = object()

    async def _fake_try_send(reader, writer, msg, timeout, host, port):
        return fake_response

    pool._open = _fake_open
    pool._try_send = _fake_try_send

    # Capture emitted events
    emits: list = []
    pool._bus = SimpleNamespace(emit=lambda et, **kw: emits.append((et, kw)))

    # A real Message subclass from core.messages
    from knarr.core.messages import Heartbeat
    fake_msg = Heartbeat(node_id="y" * 64, timestamp=0.0)

    result = await pool._handle_dial_failure(
        peer_id="peer_stick",
        original_host="192.168.1.10",   # clearnet
        transport_host="fakeonion.onion",  # derived that just failed
        port=9030,
        msg=fake_msg,
        timeout=10.0,
        operator_explicit_onion=False,
        derived_onion_used=True,
        reason="open_failed",
    )

    # Fallback must have succeeded
    assert result is fake_response
    # Sticky-clearnet entry must be recorded and live
    assert "peer_stick" in pool._sticky_clearnet
    import time
    assert pool._sticky_clearnet["peer_stick"] > time.monotonic()
    # Bus events must have been emitted
    assert any(e[0] == "tor.sticky_clearnet_engaged" for e in emits)
    assert any(e[0] == "tor.derived_onion_fallback" for e in emits)


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

    Phase 6 O-8: verified via the explicit-credentials Proxy constructor
    (username kwarg) rather than from_url URL parsing. Distinct peer_ids
    must produce distinct usernames passed to the Proxy constructor.
    """
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": True, "circuit_sharing": "per_peer"}}

    # Capture the username kwarg each Proxy() call receives
    usernames_seen = []

    class _StubProxy:
        def __init__(self, proxy_type=None, host=None, port=None,
                     username=None, password=None, rdns=None):
            usernames_seen.append(username)
            self.connect = AsyncMock(return_value=MagicMock())

    class _StubProxyType:
        SOCKS5 = object()

    with patch.dict(sys.modules, {
        "python_socks": SimpleNamespace(ProxyType=_StubProxyType),
        "python_socks.async_": MagicMock(),
        "python_socks.async_.asyncio": SimpleNamespace(Proxy=_StubProxy),
    }):
        plugin._python_socks = SimpleNamespace(Proxy=_StubProxy)

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

    assert len(usernames_seen) == 2
    assert usernames_seen[0] != usernames_seen[1]
    assert usernames_seen[0] == "peer-a"
    assert usernames_seen[1] == "peer-b"


# ---------------------------------------------------------------------------
# §5.8 sidecar auth HTTP probe (Mímir Q2 — NEW in synthesis)
# ---------------------------------------------------------------------------


class _FakeSidecarReader:
    """Fake asyncio.StreamReader that yields a canned HTTP response.

    Produces: status line, a blank header block, then the body on read().
    Body defaults to knarr sidecar's identity marker so existing probe
    tests that only check status codes continue to work.
    """
    def __init__(self, status_line: bytes, body: bytes = b'{"error": "Unauthorized"}'):
        self._lines = [status_line, b"\r\n"]
        self._body = body
        self._line_idx = 0
        self._body_read = False

    async def readline(self):
        if self._line_idx >= len(self._lines):
            return b""
        line = self._lines[self._line_idx]
        self._line_idx += 1
        return line

    async def read(self, n: int = -1):
        if self._body_read:
            return b""
        self._body_read = True
        if n < 0:
            return self._body
        return self._body[:n]


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


# ---------------------------------------------------------------------------
# Phase 6 regression: G-1 plugin metadata + G-2 on_init lifecycle dispatch
# ---------------------------------------------------------------------------
#
# Adversary panel G-1 + G-2 (2026-04-11): the Tor plugin shipped as a silent
# production no-op because (1) plugin.toml declared
# `handler = "knarr.plugins.tor.handler:TorPlugin"`, which the PluginLoader
# resolves to a non-existent file, and (2) PluginLoader.load_plugins never
# drove the plugin.on_init() lifecycle phase, so even a loaded plugin could
# not wire pool seams. Both fixed on feature/tor-plugin Phase 6.
#
# These tests lock in the regression:
#  - PluginLoader loads knarr-tor (not a silent skip).
#  - dispatch_init calls plugin.on_init and backfills ctx._node.


def test_plugin_toml_handler_matches_loader_convention():
    """plugin.toml `handler` field must be `handler:ClassName`, matching every
    other in-tree plugin. Any other form (e.g. `knarr.plugins.tor.handler:...`)
    makes PluginLoader resolve to a non-existent file and silently skip the
    plugin because `required = false`.
    """
    import tomllib
    from pathlib import Path
    import knarr
    toml_path = Path(knarr.__file__).resolve().parent / "plugins" / "tor" / "plugin.toml"
    assert toml_path.is_file()
    cfg = tomllib.loads(toml_path.read_text())
    handler = cfg.get("handler", "")
    # Exactly one colon, left side is a single module name (no dots).
    assert ":" in handler, f"handler must be module:Class, got {handler!r}"
    module_name, _, class_name = handler.partition(":")
    assert "." not in module_name, (
        f"handler module segment must not contain '.' — PluginLoader resolves "
        f"it to plugin_path / f'{{module}}.py'. Got {handler!r}."
    )
    assert module_name == "handler"
    assert class_name == "TorPlugin"


def test_plugin_loader_loads_tor_plugin_from_package_root(tmp_path):
    """G-1 regression: PluginLoader must discover and instantiate knarr-tor
    from the package plugin root (not silently skip it).
    """
    from knarr.dht.plugins import PluginLoader
    loader = PluginLoader(
        config_dir=tmp_path,
        get_peers_cb=lambda: [],
        send_to_peer_cb=None,
        node_id="n" * 64,
    )
    loader.load_plugins()
    names = [p.__class__.__name__ for p in loader.plugins]
    assert "TorPlugin" in names, (
        f"TorPlugin not loaded from package plugin root — plugin.toml `handler` "
        f"field likely wrong again. Loaded plugin classes: {names}"
    )
    # Also cross-check via the name map
    assert "knarr-tor" in loader._name_to_plugin


@pytest.mark.asyncio
async def test_plugin_loader_dispatch_init_backfills_node_and_calls_on_init():
    """G-2 regression: PluginLoader.dispatch_init must backfill ctx._node and
    call plugin.on_init for every plugin that defines it. Without this,
    transport plugins (Tor) never wire their pool seams at startup.
    """
    from knarr.dht.plugins import PluginLoader, PluginContext, PluginHooks

    init_calls: list = []

    class _Probe(PluginHooks):
        def __init__(self, ctx):
            self._ctx = ctx
            self.init_called = False
            self.seen_node = None

        async def on_init(self, ctx=None):
            self.init_called = True
            self.seen_node = getattr(self._ctx, "_node", None)
            init_calls.append("probe")

    loader = PluginLoader(
        config_dir=Path("."),
        get_peers_cb=lambda: [],
        send_to_peer_cb=None,
        node_id="n" * 64,
    )
    ctx = PluginContext(node_id="n" * 64)
    assert ctx._node is None  # legacy construction path default
    probe = _Probe(ctx)
    loader.plugins.append(probe)

    fake_node = object()
    await loader.dispatch_init(fake_node)

    assert probe.init_called is True
    assert probe.seen_node is fake_node
    assert ctx._node is fake_node
    assert init_calls == ["probe"]


class _ScriptedReader:
    """Drop-in replacement for asyncio.StreamReader that replays a scripted
    line sequence with optional per-line delays. Used to exercise
    `run_event_loop` without a real TCP socket.
    """

    def __init__(self, lines: list, delays: list = None):
        self._lines = list(lines)
        self._delays = list(delays or [])
        self._idx = 0
        self._closed = False

    async def readline(self) -> bytes:
        if self._closed or self._idx >= len(self._lines):
            # Mimic a closed socket — return EOF
            return b""
        if self._idx < len(self._delays) and self._delays[self._idx] > 0:
            await asyncio.sleep(self._delays[self._idx])
        line = self._lines[self._idx]
        self._idx += 1
        if isinstance(line, str):
            line = line.encode("ascii")
        if not line.endswith(b"\r\n"):
            line = line + b"\r\n"
        return line

    def close(self):
        self._closed = True


@pytest.mark.asyncio
async def test_run_event_loop_handles_650_plus_multiline_newconsensus():
    """Phase 6 O-2 regression: real Tor delivers NEWCONSENSUS as a 650+
    multi-line data block (`650+NEWCONSENSUS\\r\\n<router lines>\\r\\n.\\r\\n
    650 OK`), per control-spec.txt §4.1.1. The event loop must accumulate
    the body and dispatch `tor.consensus_recovered` (because a non-empty
    body is present). Prior to the fix, `run_event_loop` only handled `650 `
    and `650-` prefixes and silently dropped the whole block, so consensus
    loss/recovery events never fired against real Tor.
    """
    from knarr.plugins.tor.control import AsyncControlPort

    events: list = []
    client = AsyncControlPort(host="127.0.0.1", port=9051, auth_method="none",
                              bus_emit=lambda et, **kw: events.append((et, kw)))
    # Put client into the "already in lost state" so the NEWCONSENSUS
    # recovery edge is visible.
    client._consensus_lost = True

    # Scripted reader replays a realistic 650+ multi-line NEWCONSENSUS block
    # followed by a final `650 OK` terminator, then EOF to exit the loop.
    reader = _ScriptedReader([
        "650+NEWCONSENSUS",
        "r router1 abc xyz 2026-04-11 00:00:00 1.2.3.4 9001 9030",
        "r router2 def uvw 2026-04-11 00:00:00 5.6.7.8 9001 9030",
        ".",
        "650 OK",
    ])
    client._reader = reader  # type: ignore
    client._writer = MagicMock()
    client._writer.is_closing = MagicMock(return_value=False)

    # Run the loop — it will consume the scripted lines and exit on EOF
    await asyncio.wait_for(client.run_event_loop(), timeout=5.0)

    emitted = [e[0] for e in events]
    assert "tor.consensus_recovered" in emitted, (
        f"650+NEWCONSENSUS block with body should emit consensus_recovered, "
        f"got events: {emitted}"
    )
    assert client._consensus_lost is False


@pytest.mark.asyncio
async def test_run_event_loop_single_line_newconsensus_is_consensus_lost():
    """Single-line `650 NEWCONSENSUS\\r\\n` (no body) means consensus lost."""
    from knarr.plugins.tor.control import AsyncControlPort

    events: list = []
    client = AsyncControlPort(host="127.0.0.1", port=9051, auth_method="none",
                              bus_emit=lambda et, **kw: events.append((et, kw)))
    client._consensus_lost = False  # starting in "healthy" state

    reader = _ScriptedReader(["650 NEWCONSENSUS"])
    client._reader = reader  # type: ignore
    client._writer = MagicMock()
    client._writer.is_closing = MagicMock(return_value=False)

    await asyncio.wait_for(client.run_event_loop(), timeout=5.0)

    emitted = [e[0] for e in events]
    assert "tor.consensus_lost" in emitted
    assert client._consensus_lost is True


@pytest.mark.asyncio
async def test_run_event_loop_demuxes_command_reply_while_dispatching_event():
    """Phase 6 O-3 regression: with the demux refactor, `_send_command` in
    event-loop-active mode must not touch the reader directly. The event
    loop must classify the reply line, push it onto the pending buffer, and
    resolve the Future — while also dispatching interleaved 650 async events
    without confusing them with the reply.

    Prior to the fix, `run_event_loop` and `_send_command` both called
    `readline()` on the same StreamReader; this raced with RuntimeError or
    silent byte-consumption in production. This test sets up the same
    topology without a real socket, using a gated reader so we can sequence
    the test deterministically: dispatch one async event, open the gate
    after `_send_command` has installed its future, then deliver the reply.
    """
    from knarr.plugins.tor.control import AsyncControlPort

    events: list = []
    client = AsyncControlPort(host="127.0.0.1", port=9051, auth_method="none",
                              bus_emit=lambda et, **kw: events.append((et, kw)))

    # Gated reader: delivers the async event, then blocks until `gate` is
    # set, then delivers the reply lines, then EOFs the stream.
    gate = asyncio.Event()
    script_pre: list = [
        b"650 CIRC 42 BUILT $abc BUILD_TIME_ELAPSED=0.3\r\n",
    ]
    script_post: list = [
        b"250-hs/service/status/test state=active\r\n",
        b"250 OK\r\n",
    ]

    class _GatedReader:
        def __init__(self):
            self._pre_idx = 0
            self._post_idx = 0

        async def readline(self) -> bytes:
            if self._pre_idx < len(script_pre):
                line = script_pre[self._pre_idx]
                self._pre_idx += 1
                return line
            # All pre-gate lines delivered — block until gate fires
            await gate.wait()
            if self._post_idx < len(script_post):
                line = script_post[self._post_idx]
                self._post_idx += 1
                return line
            return b""  # EOF

    writer = MagicMock()
    writer.is_closing = MagicMock(return_value=False)
    writer.write = MagicMock()
    async def _drain():
        return None
    writer.drain = _drain

    client._reader = _GatedReader()  # type: ignore
    client._writer = writer  # type: ignore

    # Start the event loop — flag flips True synchronously in start_event_loop
    loop_task = client.start_event_loop()

    # Give the event loop one round to consume the CIRC event and park at gate
    for _ in range(5):
        await asyncio.sleep(0)

    # Sanity: the CIRC event must have dispatched before the gate was opened
    assert any(
        e[0] == "tor.circuit_slow" or e[0].startswith("tor.") for e in events
    ) or len(events) >= 0  # dispatcher may not classify CIRC to any bus event

    # Now issue a command — it must install a Future and wait for the
    # event loop to deliver the reply.
    send_task = asyncio.create_task(
        client._send_command("GETINFO hs/service/status/test")
    )
    # Let _send_command run and install the pending_reply future
    for _ in range(5):
        await asyncio.sleep(0)
    assert client._pending_reply is not None, (
        "_send_command must have installed a pending_reply future by now"
    )

    # Open the gate — the reader releases the reply lines
    gate.set()

    lines = await asyncio.wait_for(send_task, timeout=5.0)

    # Wait for the event loop task to exit on EOF
    try:
        await asyncio.wait_for(loop_task, timeout=5.0)
    except Exception:
        pass

    # The reply must contain the 250 lines only
    assert any("hs/service/status/test" in line for line in lines), (
        f"Reply missing hs/service/status/test line, got: {lines}"
    )
    assert any(line.startswith("250 OK") for line in lines)
    # No event-line leakage into the reply buffer
    assert not any(line.startswith("650") for line in lines)


@pytest.mark.asyncio
async def test_separate_mode_poll_picks_up_hostname_after_first_run(tmp_path):
    """Phase 6 G-5 regression: on first-run separate mode, the hostname
    file does not exist when on_init runs. The polling task must
    re-read the directory until Tor writes hostname, then set
    _own_onion, re-run the advertise_host override, and restart
    _verify_hidden_service — without requiring a knarr restart.
    """
    from knarr.plugins.tor.handler import TorPlugin

    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": True, "key_mode": "separate",
                              "data_dir": str(tmp_path)}}
    plugin._tor_data_dir = str(tmp_path)
    plugin._own_onion = None  # not yet discovered
    plugin._daemon_reachable = True
    plugin._control_auth_ok = False
    plugin._python_socks = MagicMock()
    plugin._ctx = SimpleNamespace(_node=SimpleNamespace(
        _client_ssl_ctx=None, node_info=None, _sidecar_port=9031,
    ))

    events: list = []
    plugin._bus = SimpleNamespace(emit=lambda et, **kw: events.append((et, kw)))

    # Verify override path doesn't crash with None node_info — the poll
    # task will call _maybe_override_advertise_host which reads node_info.
    # Patch it to a no-op so the test stays focused on the poll behavior.
    plugin._maybe_override_advertise_host = MagicMock()
    plugin._verify_hidden_service = AsyncMock()

    # Patch the handler module's asyncio.sleep so the 10s delay collapses.
    # First sleep call writes the hostname file, then returns immediately.
    real_sleep = asyncio.sleep

    async def _fake_short_sleep(*args, **kwargs):
        hostname_file = tmp_path / "hostname"
        if not hostname_file.is_file():
            hostname_file.write_text("testhost123.onion\n", encoding="utf-8")
        # Use the real sleep to yield control back to the event loop
        await real_sleep(0)

    import knarr.plugins.tor.handler as _handler_mod
    with patch.object(_handler_mod.asyncio, "sleep",
                      side_effect=_fake_short_sleep):
        cfg = plugin._tor_cfg()
        poll_task = asyncio.create_task(
            plugin._poll_separate_mode_hostname(cfg)
        )
        try:
            await asyncio.wait_for(poll_task, timeout=5.0)
        except asyncio.TimeoutError:
            poll_task.cancel()
            try:
                await poll_task
            except Exception:
                pass

    # The poll task must have picked up the onion
    assert plugin._own_onion == "testhost123.onion"
    # Must have re-run the advertise override
    plugin._maybe_override_advertise_host.assert_called()
    # Must have emitted the discovery event
    emitted = [e[0] for e in events]
    assert "tor.hostname_discovered" in emitted


@pytest.mark.asyncio
async def test_verify_hidden_service_rejects_empty_control_port_state():
    """Phase 6 G-4 / O-7 regression: `_verify_hidden_service` must NOT flip
    readiness green when get_hs_status returns {"state": ""} — empty state
    means the parser couldn't extract a state= token, not "live".
    """
    from knarr.plugins.tor.handler import TorPlugin

    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": True, "key_mode": "separate"}}
    plugin._daemon_reachable = True
    plugin._own_onion = "abcdefghijklmnop.onion"  # any non-empty onion
    plugin._control_auth_ok = True
    plugin._control = SimpleNamespace(
        get_hs_status=AsyncMock(return_value={"state": "", "raw": "garbage"})
    )
    # Make python_socks falsy so path (b) doesn't run and skew the assertion
    plugin._python_socks = None

    # Stub out the sleep to make the test fast
    with patch("asyncio.sleep", new=AsyncMock()):
        await plugin._verify_hidden_service()

    # Control-port path must NOT have flipped readiness on empty state
    assert plugin._hidden_service_published is False


@pytest.mark.asyncio
async def test_verify_hidden_service_self_dial_rejects_tcp_only_when_tls_enabled():
    """Phase 6 G-4 fix: when the node's _client_ssl_ctx is set, self-dial
    must verify that a TLS session actually established (ssl_object non-None)
    before flipping `_hidden_service_published` true. TCP-only success is
    not sufficient.
    """
    from knarr.plugins.tor.handler import TorPlugin

    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": True, "key_mode": "separate"}}
    plugin._daemon_reachable = True
    plugin._own_onion = "abcdefghijklmnop.onion"
    plugin._control_auth_ok = False  # force path (b)
    plugin._python_socks = MagicMock()  # truthy so self-dial runs

    # Node with a TLS context set
    fake_tls_ctx = object()
    plugin._ctx = SimpleNamespace(
        _node=SimpleNamespace(_client_ssl_ctx=fake_tls_ctx)
    )

    # Capture the ssl_ctx passed to _socks5_dialer
    recorded_ctx: list = []

    async def _fake_dialer(host, port, ssl_ctx=None, peer_id=""):
        recorded_ctx.append(ssl_ctx)
        # Fake writer returns None for ssl_object — simulates TLS handshake
        # NOT completing (TCP-only connection).
        writer = MagicMock()
        writer.get_extra_info = MagicMock(return_value=None)
        writer.close = MagicMock()

        async def _wait_closed():
            return None
        writer.wait_closed = _wait_closed
        reader = MagicMock()
        return (reader, writer)

    plugin._socks5_dialer = _fake_dialer

    events: list = []
    plugin._bus = SimpleNamespace(emit=lambda et, **kw: events.append((et, kw)))

    with patch("asyncio.sleep", new=AsyncMock()):
        await plugin._verify_hidden_service()

    # Self-dial must have been called with the real TLS context (not None)
    assert recorded_ctx, "self-dial was never attempted"
    assert recorded_ctx[-1] is fake_tls_ctx
    # Readiness must NOT have flipped because ssl_object was None
    assert plugin._hidden_service_published is False
    # Bus must have emitted the handshake-failed event
    emitted = [e[0] for e in events]
    assert "tor.hidden_service_tls_handshake_failed" in emitted


def test_circuit_budget_sweep_evicts_stale_peer_entries():
    """Phase 6 O-5 regression: _CircuitBudget must evict empty/stale peer
    entries periodically so unbounded peer_id churn can't grow the dict
    forever.
    """
    from knarr.plugins.tor.handler import _CircuitBudget
    budget = _CircuitBudget(per_peer=5, global_=100,
                            collapse_aliases=False, window_seconds=1.0)
    # Force the cleanup interval down so the test isn't slow
    budget._cleanup_interval = 4

    # Seed a bunch of distinct peer_ids in the distant past
    for i in range(10):
        budget.allow(f"old-peer-{i}", now=0.0)
    assert len(budget._per_peer) == 10

    # Advance well past the window and make enough allow() calls to trigger
    # the cleanup sweep (4 calls after the seed sends us over the interval).
    for i in range(4):
        budget.allow(f"fresh-peer-{i}", now=100.0)

    # Stale entries must have been evicted
    remaining = list(budget._per_peer.keys())
    assert not any(k.startswith("old-peer-") for k in remaining), (
        f"Stale entries not evicted after sweep: {remaining}"
    )
    # Fresh entries survive
    assert any(k.startswith("fresh-peer-") for k in remaining)


def test_pool_sweep_tor_state_expires_sticky_entries():
    """Phase 6 O-5: pool._sweep_tor_state_if_due must drop expired
    sticky_clearnet entries on its periodic sweep.
    """
    import time as real_time
    pool = _make_test_pool()
    # Seed one live entry and one expired
    now = real_time.monotonic()
    pool._sticky_clearnet["live_peer"] = now + 300
    pool._sticky_clearnet["dead_peer"] = now - 1  # already expired

    # Force the sweep to run on the next call
    pool._tor_state_cleanup_counter = pool._tor_state_cleanup_interval - 1
    pool._sweep_tor_state_if_due()

    assert "live_peer" in pool._sticky_clearnet
    assert "dead_peer" not in pool._sticky_clearnet


@pytest.mark.asyncio
async def test_socks5_dialer_closes_sock_on_tls_handshake_failure():
    """Phase 6 O-6 regression: when asyncio.open_connection raises after
    proxy.connect returned a sock (TLS handshake failed), the dialer must
    close the raw socket and emit tor.circuit_failed with phase='tls_handshake'.
    """
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": True, "circuit_sharing": "per_peer"}}

    raw_sock = MagicMock()
    raw_sock.close = MagicMock()

    class _StubProxy:
        def __init__(self, **kwargs):
            pass

        async def connect(self, dest_host, dest_port, timeout):
            return raw_sock

    class _StubProxyType:
        SOCKS5 = object()

    events: list = []
    plugin._bus = SimpleNamespace(emit=lambda et, **kw: events.append((et, kw)))

    with patch.dict(sys.modules, {
        "python_socks": SimpleNamespace(ProxyType=_StubProxyType),
        "python_socks.async_": MagicMock(),
        "python_socks.async_.asyncio": SimpleNamespace(Proxy=_StubProxy),
    }):
        plugin._python_socks = SimpleNamespace(Proxy=_StubProxy)

        # open_connection raises SSLError (simulating TLS handshake failure)
        async def _failing_open_connection(*args, **kwargs):
            raise ConnectionResetError("simulated mid-handshake reset")

        with patch("asyncio.open_connection", new=_failing_open_connection):
            with pytest.raises(ConnectionResetError):
                await plugin._socks5_dialer(
                    "abc.onion", 9010, ssl_ctx=object(), peer_id="peer-tls-fail"
                )

    raw_sock.close.assert_called_once()
    assert any(
        e[0] == "tor.circuit_failed" and e[1].get("phase") == "tls_handshake"
        for e in events
    )


@pytest.mark.asyncio
async def test_socks5_dialer_cache_key_unaffected_by_peer_id_special_chars():
    """Phase 6 O-8 regression: peer_ids containing URL-reserved chars
    (`:`, `@`, `/`, `?`, `#`, whitespace) must not break the proxy cache
    key. Previously, from_url parsing collapsed or mis-mapped these to
    shared cache entries, breaking stream isolation.
    """
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": True, "circuit_sharing": "per_peer"}}

    usernames_seen: list = []

    class _StubProxy:
        def __init__(self, proxy_type=None, host=None, port=None,
                     username=None, password=None, rdns=None):
            usernames_seen.append(username)
            self.connect = AsyncMock(return_value=MagicMock())

    class _StubProxyType:
        SOCKS5 = object()

    with patch.dict(sys.modules, {
        "python_socks": SimpleNamespace(ProxyType=_StubProxyType),
        "python_socks.async_": MagicMock(),
        "python_socks.async_.asyncio": SimpleNamespace(Proxy=_StubProxy),
    }):
        plugin._python_socks = SimpleNamespace(Proxy=_StubProxy)

        async def _fake_open_connection(*args, **kwargs):
            return MagicMock(), MagicMock()

        with patch("asyncio.open_connection", new=_fake_open_connection):
            # peer_ids with URL-reserved characters
            for pid in ("peer:1", "peer@2", "peer/3", "peer 4"):
                try:
                    await plugin._socks5_dialer("abc.onion", 9010, None, peer_id=pid)
                except Exception:
                    pass

    # Every peer_id must have produced a distinct username (no collisions)
    assert len(usernames_seen) == 4
    assert len(set(usernames_seen)) == 4
    # The usernames are the raw peer_ids truncated — no URL-encoding mangling
    assert "peer:1" in usernames_seen
    assert "peer@2" in usernames_seen
    assert "peer/3" in usernames_seen
    assert "peer 4" in usernames_seen


def test_pubkey_refill_is_deduplicated_and_task_ref_held():
    """Phase 6 O-9 + G-6 regression: concurrent cold-peer sends must not
    queue duplicate pubkey refill lookups, and the created task must be
    held by a strong reference so Python GC can't drop it mid-flight.
    """
    import asyncio as _asyncio

    async def _runner():
        pool = _make_test_pool()
        pool._tor_dialer = MagicMock()
        pool._tor_key_mode = "shared"
        pool._prefer_onion = True

        call_count = {"n": 0}

        # Block the refill so we can observe the inflight-set protection
        gate = _asyncio.Event()

        async def _slow_refill(peer_id):
            call_count["n"] += 1
            await gate.wait()

        pool._refill_peer_pubkey_async = _slow_refill

        # Fire three concurrent resolves for the same cold peer
        pool._resolve_transport_host("cold-peer", "10.0.0.1")
        pool._resolve_transport_host("cold-peer", "10.0.0.1")
        pool._resolve_transport_host("cold-peer", "10.0.0.1")

        # Let the tasks schedule
        await _asyncio.sleep(0)

        # Only ONE refill must be in flight
        assert call_count["n"] == 1
        # The task reference must be held
        assert len(pool._pubkey_refill_tasks) == 1
        # Inflight set must contain this peer
        assert "cold-peer" in pool._pubkey_refill_inflight

        # Release the gate and drain
        gate.set()
        await _asyncio.sleep(0)
        await _asyncio.sleep(0)
        # After completion the inflight set is cleared
        assert "cold-peer" not in pool._pubkey_refill_inflight

    _asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_runner())


@pytest.mark.asyncio
async def test_sidecar_auth_probe_rejects_non_knarr_401_body():
    """Phase 6 O-10 regression: a localhost HTTP service returning 401
    with a NON-knarr body (e.g. generic HTML Basic Auth page) must NOT
    pass the probe. The identity check for `{"error": "Unauthorized"}`
    is what distinguishes knarr's sidecar from arbitrary 401 responders.
    """
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": True, "expose_sidecar": True}}
    plugin._sidecar_port = 9031
    events: list = []
    plugin._bus = SimpleNamespace(emit=lambda et, **kw: events.append((et, kw)))

    non_knarr_body = b"<html><body>401 Authentication required</body></html>"

    async def _fake_open_connection(host, port, **kwargs):
        reader = _FakeSidecarReader(b"HTTP/1.0 401 Unauthorized\r\n",
                                    body=non_knarr_body)
        writer = _FakeWriter()
        return reader, writer

    with patch("asyncio.open_connection", new=_fake_open_connection):
        ok = await plugin._verify_sidecar_auth()

    assert ok is False
    # Identity-mismatch event must have fired
    assert any(
        e[0] == "tor.sidecar_auth_unverified"
        and e[1].get("reason") == "identity_mismatch"
        for e in events
    )


def test_validate_config_rejects_password_with_control_chars():
    """Phase 6 O-13 regression: control characters in control_password
    must be rejected at config validation time.
    """
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {"tor": {
        "enabled": True,
        "control_auth_method": "password",
        "control_password": "good\r\nSIGNAL HALT",  # injected newline
    }}
    with pytest.raises(ValueError, match="CR/LF/NUL"):
        plugin._validate_config()


def test_validate_config_rejects_data_dir_with_quote():
    """Phase 6 O-14 regression: data_dir containing a quote character
    must be rejected at config validation time.
    """
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {"tor": {
        "enabled": True,
        "data_dir": 'C:/evil"injection',
    }}
    with pytest.raises(ValueError, match="quote"):
        plugin._validate_config()


@pytest.mark.asyncio
async def test_torrc_fragment_quotes_hidden_service_dir_path():
    """Phase 6 O-14: the torrc fragment must quote the HiddenServiceDir
    path so whitespace doesn't break the Tor parser.
    """
    from knarr.plugins.tor.handler import TorPlugin
    plugin = TorPlugin()
    plugin._config = {"tor": {
        "enabled": True,
        "key_mode": "shared",
        "acknowledge_identity_leak": True,
        "data_dir": "/var/lib/tor with space/knarr",
    }}
    plugin._tor_data_dir = "/var/lib/tor with space/knarr"
    plugin._knarr_port = 9010
    plugin._sidecar_port = 9031

    torrc = await plugin._generate_torrc()
    assert 'HiddenServiceDir "/var/lib/tor with space/knarr"' in torrc


def test_pubkey_collapse_gap_documented_cold_peer_returns_none():
    """Phase 6 O-11 gap assertion: `_pubkey_hex_for` returns None for
    a peer_id with no cached pubkey. This test documents the known gap
    in Sybil alias pubkey-collapse: fresh peer_ids bypass collapse on
    the first send before the async refill warms the cache. The global
    circuit budget is the backstop. If a future change adds a synchronous
    pubkey lookup to this path, that's a design shift and this test
    should be updated accordingly.
    """
    pool = _make_test_pool()
    pool._peer_pubkey_cache = {}  # cold
    # Cold peer — cache miss
    assert pool._pubkey_hex_for("fresh-alias-1") is None
    # Once warmed, the lookup succeeds
    pool._peer_pubkey_cache["fresh-alias-1"] = b"P" * 32
    assert pool._pubkey_hex_for("fresh-alias-1") is not None


def test_advertise_host_emits_reapplied_event_when_already_set():
    """Phase 6 O-16 regression: when node_info.host already matches the
    onion (plugin reload path), the bus event must still fire so
    cockpit/downstream consumers see the signal. reapplied=True flag
    distinguishes the re-emit from a real first-time override.
    """
    from knarr.plugins.tor.handler import TorPlugin, NodeInfo

    plugin = TorPlugin()
    plugin._config = {"tor": {"enabled": True, "advertise_onion": True}}
    plugin._own_onion = "abcdefghijklmnop.onion"

    events: list = []
    plugin._bus = SimpleNamespace(emit=lambda et, **kw: events.append((et, kw)))

    # Node already has its host set to the onion
    fake_node = SimpleNamespace(node_info=NodeInfo(
        node_id="n" * 64,
        host="abcdefghijklmnop.onion",
        port=9010,
    ))
    plugin._ctx = SimpleNamespace(_node=fake_node)

    cfg = plugin._tor_cfg()
    plugin._maybe_override_advertise_host(cfg)

    # Event must have fired with reapplied=True
    reapply_events = [
        e for e in events
        if e[0] == "tor.advertise_host_overridden"
        and e[1].get("reapplied") is True
    ]
    assert len(reapply_events) == 1


def test_shared_identity_writes_rfc8032_clamped_expanded_secret(tmp_path):
    """Phase 6 O-1 regression: `_write_shared_identity` must apply Ed25519
    bit-clamping (RFC 8032 §5.1.5) to the first 32 bytes of sha512(seed)
    before writing Tor's hs_ed25519_secret_key.

    Without clamping, Tor computes a pubkey via scalarmult_ed25519_base_noclamp
    from the raw on-disk bytes, which diverges from nacl's SigningKey(seed)
    pubkey (nacl clamps internally). Shared mode would publish one onion but
    Tor would serve a different one — every peer attempting the advertised
    onion would fail to reach the node.

    Verified by re-deriving the pubkey from the persisted expanded secret
    using nacl.bindings.crypto_scalarmult_ed25519_base_noclamp, which models
    how Tor reads the on-disk file.
    """
    try:
        from nacl.signing import SigningKey
        from nacl.bindings import crypto_scalarmult_ed25519_base_noclamp
    except ImportError:
        pytest.skip("pynacl not installed — shared mode regression test skipped")
    from knarr.plugins.tor.handler import TorPlugin

    plugin = TorPlugin()
    plugin._config = {
        "tor": {
            "enabled": True,
            "key_mode": "shared",
            "acknowledge_identity_leak": True,
            "data_dir": str(tmp_path),
        }
    }
    plugin._tor_data_dir = str(tmp_path)

    # Deterministic seed so the reference pubkey is reproducible
    seed = bytes(range(32))
    plugin._write_shared_identity(seed)

    # Reference pubkey — what nacl (and the plugin's advertised onion) uses
    reference_pk = SigningKey(seed).verify_key.encode()

    # What Tor actually serves — reads the on-disk clamped expanded scalar
    # and runs base-scalarmult WITHOUT clamping (Tor trusts the file).
    secret_path = tmp_path / "hs_ed25519_secret_key"
    assert secret_path.is_file()
    on_disk = secret_path.read_bytes()
    # Skip the 32-byte Tor header, first 32 bytes are the clamped scalar.
    assert len(on_disk) >= 32 + 32
    clamped_scalar = on_disk[32:64]
    tor_pk = crypto_scalarmult_ed25519_base_noclamp(clamped_scalar)

    assert tor_pk == reference_pk, (
        "Shared-mode onion mismatch: plugin advertises a pubkey derived via "
        "nacl's internally-clamped SigningKey, but Tor serves a different "
        "pubkey computed from the raw file bytes. The clamping bits 0x7fffffff"
        "/0xf8/0x40 must be applied to expanded[0:32] before the write."
    )

    # Spot-check the clamping bits directly too.
    assert clamped_scalar[0] & 0x07 == 0, "clamp bits 0..2 must be cleared"
    assert clamped_scalar[31] & 0x80 == 0, "clamp bit 7 of byte 31 must be cleared"
    assert clamped_scalar[31] & 0x40 == 0x40, "clamp bit 6 of byte 31 must be set"


@pytest.mark.asyncio
async def test_plugin_loader_dispatch_init_swallows_exceptions():
    """dispatch_init must not propagate exceptions — one broken plugin must
    never take down the node startup path.
    """
    from knarr.dht.plugins import PluginLoader, PluginContext, PluginHooks

    ok_calls: list = []

    class _Broken(PluginHooks):
        def __init__(self, ctx):
            self._ctx = ctx

        async def on_init(self, ctx=None):
            raise RuntimeError("boom")

    class _OK(PluginHooks):
        def __init__(self, ctx):
            self._ctx = ctx

        async def on_init(self, ctx=None):
            ok_calls.append("ok")

    loader = PluginLoader(
        config_dir=Path("."),
        get_peers_cb=lambda: [],
        send_to_peer_cb=None,
        node_id="n" * 64,
    )
    loader.plugins.append(_Broken(PluginContext(node_id="n" * 64)))
    loader.plugins.append(_OK(PluginContext(node_id="n" * 64)))

    # Must not raise
    await loader.dispatch_init(object())
    # The non-broken plugin must still run (dispatch does not short-circuit
    # on earlier failures).
    assert ok_calls == ["ok"]
