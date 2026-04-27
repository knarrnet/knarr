"""Tor transport plugin — SPEC-tor-plugin.md v1.1.

Synthesized from the 2026-04-11 blind parallel dev panel.
Full per-component rationale: F:\\thing\\specs\\SYNTHESIS-tor-plugin.md

Components (spec §2):
  1. Outbound .onion SOCKS5 routing via plugin-registered dialer on the pool
  2. Tor daemon detection via control port PROTOCOLINFO (O-6)
  3. Ed25519 identity modes — separate (default) / shared (opt-in + interlock)
  4. Runtime-only advertise_host override (O-13, dataclass replacement)
  5. Shared-mode-only dual-stack resolution in the pool (§2.5, F-1)
  6. Bus events for cockpit observability
  7. Sidecar auth HTTP probe (§5.8 Mímir Q2 carve-out — raw asyncio, no aiohttp dep)

Panel source picks:
- GPT (proposed-c): base structure, config defaults, NodeInfo dataclass replacement,
  _peer_pubkey_lookup using real storage.get_pubkey_by_node_id
- Opus (proposed-a): _CircuitBudget class verbatim, _verify_hidden_service state machine
- Sonnet (proposed-b): graceful shared-mode fallback pattern
- NEW in synthesis: sidecar auth HTTP probe (Mímir Q2, not in any seat)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from knarr.core.models import NodeInfo
from knarr.dht.plugins import PluginContext, PluginHooks

# Dual-import pattern (same as 10-bcw plugin):
# - Package form (tests that do `from knarr.plugins.tor.handler import ...`)
#   resolves `.control` against the namespace package, works.
# - PluginLoader form (loads handler.py as a free-standing module via
#   spec_from_file_location) has no parent package, so `.control` ImportErrors.
#   V035-001 sibling pre-loader already dropped `control` into sys.modules as
#   a bare-name module, so the fallback succeeds.
try:
    from .control import AsyncControlPort, ControlPortError
except ImportError:
    from control import AsyncControlPort, ControlPortError  # type: ignore


log = logging.getLogger("knarr.plugin.tor")


_TOR_SECRET_HEADER = b"== ed25519v1-secret: type0 ==\x00\x00\x00"


# ---------------------------------------------------------------------------
# Config defaults (§4 — v1.1 key_mode flip + safety interlocks)
# ---------------------------------------------------------------------------


_DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "socks_host": "127.0.0.1",
    "socks_port": 9050,
    "data_dir": "",
    # v1.1 F-1 + O-1 + O-2 — separate is the safe default
    "key_mode": "separate",
    "acknowledge_identity_leak": False,
    "advertise_onion": True,
    "prefer_onion": False,
    "derived_onion_fallback": True,
    "derived_onion_fallback_window": 300,
    "circuit_sharing": "per_peer",
    "max_circuits_per_peer_per_minute": 10,
    "max_circuits_global_per_minute": 60,
    "collapse_pubkey_aliases": True,
    "slow_circuit_warning_ms": 5000,
    # v1.1 O-9 sidecar exposure gate (default off, explicit opt-in)
    "expose_sidecar": False,
    # v1.1 Mímir Q2 — belt-and-suspenders HTTP auth probe override
    "sidecar_allow_unauth": False,
    # v1.1 §2.2.1 control port
    "control_port": 9051,
    "control_auth_method": "cookie",
    "control_cookie_file": "",
    "control_password": "",
}


# ---------------------------------------------------------------------------
# Onion address derivation (§2.3) — pure function, no plugin state
# ---------------------------------------------------------------------------


def onion_address_from_pubkey(pubkey: bytes) -> str:
    """Derive a Tor v3 onion address from a 32-byte Ed25519 public key.

    Format (rend-spec-v3.txt §6):
        onion_address = base32(pubkey || checksum || version).lower() + ".onion"
        checksum      = SHA3-256(".onion checksum" || pubkey || version)[:2]
        version       = 0x03
    """
    if not isinstance(pubkey, (bytes, bytearray)) or len(pubkey) != 32:
        raise ValueError("Ed25519 public key must be exactly 32 bytes")
    version = b"\x03"
    checksum = hashlib.sha3_256(b".onion checksum" + bytes(pubkey) + version).digest()[:2]
    raw = bytes(pubkey) + checksum + version  # 35 bytes
    b32 = base64.b32encode(raw).decode("ascii").lower().rstrip("=")
    return f"{b32}.onion"


# ---------------------------------------------------------------------------
# Circuit budget tracker (§5.5 — F-4)
# ---------------------------------------------------------------------------


class _CircuitBudget:
    """Sliding-window rate limiter with three buckets: per-peer, global,
    and pubkey-collapse (aliased peer_ids sharing one pubkey share a bucket).

    From Opus's proposed-a verbatim — the class is well-tested and
    self-contained. Synthesis wires it into the pool's dial path via
    ConnectionPool._record_circuit_attempt (which delegates to this class's
    allow() method when set by TorPlugin._configure_pool).

    Returns (allowed: bool, reason: str). reason is "" on success or
    "per_peer" / "global" / "pubkey_collapse" for distinct denial telemetry.
    """

    def __init__(
        self,
        per_peer: int = 10,
        global_: int = 60,
        collapse_aliases: bool = True,
        window_seconds: float = 60.0,
    ) -> None:
        self.per_peer = int(per_peer)
        self.global_ = int(global_)
        self.collapse_aliases = bool(collapse_aliases)
        self.window = float(window_seconds)
        self._per_peer: Dict[str, Deque[float]] = defaultdict(deque)
        self._per_pubkey: Dict[str, Deque[float]] = defaultdict(deque)
        self._global: Deque[float] = deque()
        # Phase 6 O-5 fix: call counter drives opportunistic eviction of
        # stale entries. Prevents unbounded growth of _per_peer and
        # _per_pubkey under peer_id / pubkey churn (Sybil attack or normal
        # long-running node seeing many transient peers).
        self._call_count: int = 0
        self._cleanup_interval: int = 128

    def _prune(self, dq: Deque[float], now: float) -> None:
        cutoff = now - self.window
        while dq and dq[0] < cutoff:
            dq.popleft()

    def _sweep_stale(self, now: float) -> None:
        """Phase 6 O-5: evict empty-after-prune entries from per-peer and
        per-pubkey dicts. Called every `_cleanup_interval` allow() calls.
        """
        cutoff = now - self.window
        for bucket in (self._per_peer, self._per_pubkey):
            # Collect stale keys first — can't mutate dict during iteration
            stale = [
                key for key, dq in bucket.items()
                if not dq or dq[-1] < cutoff
            ]
            for key in stale:
                bucket.pop(key, None)

    def allow(
        self,
        peer_id: str,
        pubkey_hex: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """Check 3 buckets and record a new circuit if allowed."""
        t = now if now is not None else time.monotonic()

        # Phase 6 O-5: periodic stale-entry sweep
        self._call_count += 1
        if self._call_count >= self._cleanup_interval:
            self._call_count = 0
            self._sweep_stale(t)

        self._prune(self._global, t)
        if len(self._global) >= self.global_:
            return False, "global"

        peer_dq = self._per_peer[peer_id]
        self._prune(peer_dq, t)
        if len(peer_dq) >= self.per_peer:
            return False, "per_peer"

        if self.collapse_aliases and pubkey_hex:
            pk_dq = self._per_pubkey[pubkey_hex]
            self._prune(pk_dq, t)
            if len(pk_dq) >= self.per_peer:
                return False, "pubkey_collapse"
            pk_dq.append(t)

        peer_dq.append(t)
        self._global.append(t)
        return True, ""


# ---------------------------------------------------------------------------
# TorPlugin
# ---------------------------------------------------------------------------


class TorPlugin(PluginHooks):
    """Tor transport plugin — SOCKS5 routing + hidden service management."""

    def __init__(
        self,
        ctx: Optional[PluginContext] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Two-mode constructor. Production: TorPlugin(ctx, config).
        Test/probe: TorPlugin() — no ctx, default config filled in via _config assignment.
        """
        self._ctx = ctx
        # Store as {"tor": {...}} so tests can do plugin._config = {"tor": {"enabled": True}}
        if config is None:
            self._config: Dict[str, Any] = {"tor": dict(_DEFAULT_CONFIG)}
        elif "tor" in config and isinstance(config["tor"], dict):
            merged = dict(_DEFAULT_CONFIG)
            merged.update(config["tor"])
            self._config = {"tor": merged}
        else:
            merged = dict(_DEFAULT_CONFIG)
            merged.update(config)
            self._config = {"tor": merged}

        self._bus: Any = getattr(ctx, "bus", None) if ctx is not None else None

        # Runtime state
        self._daemon_reachable: bool = False
        self._hidden_service_published: bool = False
        self._own_onion: Optional[str] = None
        self._tor_version: str = ""
        self._control_auth_ok: bool = False
        self._dependency_loaded: bool = False
        self._last_consensus_check: float = 0.0
        self._last_hs_verify: float = 0.0
        self._circuit_count: int = 0
        self._sidecar_auth_verified: bool = False

        self._control: Optional[AsyncControlPort] = None
        self._control_task: Optional[asyncio.Task] = None
        self._self_dial_task: Optional[asyncio.Task] = None
        # Phase 6 G-5: background task that polls the Tor hostname file in
        # separate mode until Tor writes it (can take minutes after fresh
        # data dir setup).
        self._separate_mode_poll_task: Optional[asyncio.Task] = None

        # Lazy-imported python-socks module handle; None until on_init probe
        self._python_socks: Any = None

        # Discovered at on_init time from ctx._node
        self._knarr_port: int = 9010
        self._sidecar_port: int = 9031
        self._tor_data_dir: str = ""
        self._self_pubkey: Optional[bytes] = None

        # Budget instance (Opus's class), wired into the pool in _configure_pool
        self._budget: Optional[_CircuitBudget] = None

        # Proxy cache keyed by the full SOCKS5 URL. SYNTHESIS §8 bug #2 fix
        # (Mímir follow-up 1): avoid reconstructing python_socks.Proxy on
        # every dial. Per-peer circuit isolation is preserved because each
        # peer_id produces a distinct URL (different username), so the cache
        # key is just the URL string. Cleared on plugin shutdown.
        # Phase 6 O-5 fix: bounded FIFO cap prevents unbounded growth under
        # peer_id churn in per_peer isolation mode. 256 is well above the
        # typical concurrent connection count and keeps churn eviction
        # cheap.
        self._proxy_cache: Dict[str, Any] = {}
        self._proxy_cache_max: int = 256

        # B-05: pre-override advertise host snapshot for clean restoration on
        # shutdown. Set the first time `_maybe_override_advertise_host` rewrites
        # node_info.host, cleared after restoration. Prevents the onion address
        # from lingering in node_info after the plugin stops.
        self._pre_override_host: Optional[str] = None

    # ------------------------------------------------------------------
    # Static helpers (API contract)
    # ------------------------------------------------------------------

    @staticmethod
    def get_default_config() -> Dict[str, Any]:
        """Return a fresh copy of the default [tor] config block."""
        return dict(_DEFAULT_CONFIG)

    @staticmethod
    def onion_address_from_pubkey(pubkey: bytes) -> str:
        """Re-export of the module-level helper as a classmethod for API symmetry."""
        return onion_address_from_pubkey(pubkey)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _tor_cfg(self) -> Dict[str, Any]:
        """Return effective [tor] config — merges test-supplied fields over defaults."""
        raw = self._config.get("tor", {}) if isinstance(self._config, dict) else {}
        merged = dict(_DEFAULT_CONFIG)
        if isinstance(raw, dict):
            merged.update(raw)
        return merged

    def _validate_config(self) -> None:
        """Enforce config-level safety interlocks. Raises ValueError on violation."""
        cfg = self._tor_cfg()
        if not cfg.get("enabled", False):
            return
        mode = cfg.get("key_mode", "separate")
        if mode not in ("separate", "shared"):
            raise ValueError(f"[tor] key_mode must be 'separate' or 'shared', got {mode!r}")
        if mode == "shared" and not cfg.get("acknowledge_identity_leak", False):
            raise ValueError(
                "[tor] key_mode='shared' requires acknowledge_identity_leak=true — "
                "enabling shared mode publishes clearnet_nodeid ↔ onion_address to "
                "every peer that has seen your pubkey. See SPEC-tor-plugin.md §5.1."
            )
        circuit_sharing = str(cfg.get("circuit_sharing", "per_peer")).lower()
        if circuit_sharing not in ("per_peer", "global"):
            raise ValueError(f"[tor] circuit_sharing must be 'per_peer' or 'global', got {circuit_sharing!r}")
        auth_method = str(cfg.get("control_auth_method", "cookie")).lower()
        if auth_method not in ("cookie", "password", "none"):
            raise ValueError(f"[tor] control_auth_method must be 'cookie'/'password'/'none', got {auth_method!r}")

        # Phase 6 O-13 fix: reject passwords containing control characters.
        # `\r` `\n` `\x00` in the password would inject command separators
        # into the AUTHENTICATE wire line (Tor control protocol is line-
        # delimited), allowing an attacker with config-file write access to
        # execute arbitrary control commands. The escaping at control.py:256
        # only handles `\\` and `"`.
        control_password = cfg.get("control_password", "") or ""
        if any(ch in control_password for ch in ("\r", "\n", "\x00")):
            raise ValueError(
                "[tor] control_password must not contain CR/LF/NUL — these "
                "are Tor control protocol command separators."
            )

        # Phase 6 O-14 fix: reject data_dir containing " or CR/LF/NUL. The
        # torrc fragment emits `HiddenServiceDir "<data_dir>"`, and these
        # characters would either break the Tor parser or allow directive
        # injection. Also requires that data_dir is a non-empty string if
        # set (empty string is treated as "use default").
        data_dir = cfg.get("data_dir", "") or ""
        if data_dir and any(ch in data_dir for ch in ('"', "\r", "\n", "\x00")):
            raise ValueError(
                "[tor] data_dir must not contain quote / CR / LF / NUL — "
                "these break the torrc fragment HiddenServiceDir line."
            )

    # ------------------------------------------------------------------
    # Readiness state split (§2.2 step 5 — F-5 + O-3)
    # ------------------------------------------------------------------

    def is_tor_daemon_reachable(self) -> bool:
        """True when Tor daemon is confirmed via PROTOCOLINFO handshake."""
        if not self._tor_cfg().get("enabled", False):
            return False
        return bool(self._daemon_reachable)

    def is_tor_hidden_service_published(self) -> bool:
        """True when the hidden service is verified live (control port OR self-dial)."""
        if not self._tor_cfg().get("enabled", False):
            return False
        return bool(self._hidden_service_published)

    def get_own_onion_address(self) -> Optional[str]:
        """Return this node's .onion address, or None if not yet known."""
        if self._own_onion:
            return self._own_onion
        cfg = self._tor_cfg()
        if cfg.get("key_mode") == "shared" and self._self_pubkey is not None:
            try:
                self._own_onion = self.onion_address_from_pubkey(self._self_pubkey)
                return self._own_onion
            except Exception:
                return None
        # Separate mode — read from hostname file
        data_dir = cfg.get("data_dir") or self._tor_data_dir
        if data_dir:
            hostname = Path(data_dir) / "hostname"
            if hostname.is_file():
                try:
                    value = hostname.read_text(encoding="utf-8").strip().split()[0]
                    if value.endswith(".onion"):
                        self._own_onion = value
                        return value
                except (OSError, IndexError):
                    pass
        return None

    def get_tor_status(self) -> Dict[str, Any]:
        """Return the full status dict for cockpit Tor panel."""
        cfg = self._tor_cfg()
        return {
            "enabled": bool(cfg.get("enabled", False)),
            "key_mode": cfg.get("key_mode", "separate"),
            "daemon_reachable": self.is_tor_daemon_reachable(),
            "hidden_service_published": self.is_tor_hidden_service_published(),
            "own_onion": self.get_own_onion_address(),
            "socks_port": int(cfg.get("socks_port", 9050)),
            "control_port": int(cfg.get("control_port", 9051)),
            "control_auth_ok": bool(self._control_auth_ok),
            "circuit_count": int(self._circuit_count),
            "last_consensus_check": float(self._last_consensus_check),
            "last_hs_verify": float(self._last_hs_verify),
            "dependency_loaded": bool(self._dependency_loaded),
            "tor_version": self._tor_version,
            "sidecar_auth_verified": bool(self._sidecar_auth_verified),
        }

    # ------------------------------------------------------------------
    # Bus emit
    # ------------------------------------------------------------------

    def _emit(self, event_type: str, **fields: Any) -> None:
        """Tolerant bus emit — None bus is a silent no-op."""
        bus = self._bus
        if bus is None:
            return
        try:
            bus.emit(event_type, **fields)
        except Exception as e:
            log.debug("TOR_BUS_EMIT_FAIL event=%s err=%s", event_type, e)

    # ------------------------------------------------------------------
    # Sidecar auth HTTP probe (§5.8 — Mímir Q2 carve-out, synthesis NEW)
    # ------------------------------------------------------------------

    async def _verify_sidecar_auth(self) -> bool:
        """HTTP probe the sidecar's root endpoint; expect 401 Unauthorized.

        Belt-and-suspenders over the config-level expose_sidecar gate. If the
        operator enabled expose_sidecar but the sidecar is actually serving
        unauthed content (runtime misconfiguration, reverse-proxy stripping
        auth, etc.), this probe catches it before the onion exposure fires.

        Raw asyncio HTTP/1.0 — no aiohttp dependency. Mímir verdict 2026-04-11:
        "a new runtime dependency just for a sidecar probe is worth avoiding."

        Returns:
          True  — 401 received → auth enforced → safe to expose via hidden service
          False — 200 received (LEAK) OR any other status OR connection error

        On False, emits one of: tor.sidecar_unauth_detected (200) /
        tor.sidecar_auth_unverified (other status or connection error).
        """
        sidecar_host = "127.0.0.1"
        sidecar_port = self._sidecar_port
        if not sidecar_port:
            self._emit("tor.sidecar_auth_unverified", reason="no sidecar port configured")
            return False

        # Raw HTTP/1.0 probe of the root endpoint.
        # Root is the most defensive probe target — sidecar's _verify_auth
        # runs BEFORE routing (confirmed at clean tree sidecar.py:191), so
        # ANY path returns 401 when auth is enforced. A 200 on / would mean
        # the sidecar routed through to some handler without auth check,
        # which is an unambiguous leak signal.
        request = (
            f"GET / HTTP/1.0\r\n"
            f"Host: {sidecar_host}:{sidecar_port}\r\n"
            f"User-Agent: knarr-tor-plugin-auth-probe\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("ascii")

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(sidecar_host, sidecar_port),
                timeout=3.0,
            )
        except (asyncio.TimeoutError, OSError, ConnectionError) as e:
            self._emit(
                "tor.sidecar_auth_unverified",
                sidecar_port=sidecar_port,
                reason=f"connect_failed: {e}",
            )
            return False

        try:
            writer.write(request)
            await asyncio.wait_for(writer.drain(), timeout=2.0)
            # Read the status line only
            try:
                status_line_raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
            except asyncio.TimeoutError:
                self._emit(
                    "tor.sidecar_auth_unverified",
                    sidecar_port=sidecar_port,
                    reason="status_line_timeout",
                )
                return False
            status_line = status_line_raw.decode("ascii", errors="replace").strip()
            # Status line format: "HTTP/1.X NNN TEXT"
            parts = status_line.split(" ", 2)
            if len(parts) < 2 or not parts[1].isdigit():
                self._emit(
                    "tor.sidecar_auth_unverified",
                    sidecar_port=sidecar_port,
                    reason=f"malformed_status: {status_line!r}",
                )
                return False
            status_code = int(parts[1])
            if status_code == 401:
                # Phase 6 O-10 fix: 401 alone is not sufficient evidence
                # that the listener is knarr's sidecar — any localhost
                # HTTP service with auth returns 401. Read the response
                # body (sidecar returns `{"error": "Unauthorized"}` JSON
                # at sidecar.py:192) and verify the sidecar-specific
                # marker is present.
                body = await self._read_http_body(reader)
                if not self._body_matches_sidecar_identity(body):
                    self._emit(
                        "tor.sidecar_auth_unverified",
                        sidecar_port=sidecar_port,
                        reason="identity_mismatch",
                        body_head=body[:64].decode("ascii", errors="replace"),
                    )
                    return False
                # Auth enforced + identity confirmed — safe to expose
                self._emit(
                    "tor.sidecar_auth_verified",
                    sidecar_port=sidecar_port,
                    status_code=401,
                )
                return True
            if status_code == 200:
                # LEAK — refuse exposure
                self._emit(
                    "tor.sidecar_unauth_detected",
                    sidecar_port=sidecar_port,
                    status_code=200,
                    reason="sidecar returned 200 without auth — unauth exposure risk",
                )
                return False
            # Any other status: fail closed
            self._emit(
                "tor.sidecar_auth_unverified",
                sidecar_port=sidecar_port,
                status_code=status_code,
                reason=f"unexpected status {status_code} — failing closed",
            )
            return False
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _read_http_body(reader: asyncio.StreamReader,
                              max_bytes: int = 1024,
                              timeout: float = 2.0) -> bytes:
        """Read past HTTP/1.x headers and return up to `max_bytes` of body.

        The probe sends `Connection: close`, so we can safely read to EOF
        after consuming the header block. Bounded by `max_bytes` so a
        malicious listener can't stream gigabytes to DoS us.
        """
        try:
            # Skip headers — read until blank CRLF line
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=timeout)
                if not line or line in (b"\r\n", b"\n"):
                    break
            # Read body up to max_bytes
            body = await asyncio.wait_for(reader.read(max_bytes), timeout=timeout)
            return body
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return b""

    @staticmethod
    def _body_matches_sidecar_identity(body: bytes) -> bool:
        """Phase 6 O-10: knarr sidecar returns `{"error": "Unauthorized"}`
        on 401 (sidecar.py:192). This exact JSON shape is the identity
        marker — a random local HTTP service returning 401 is extremely
        unlikely to produce an identical body.
        """
        if not body:
            return False
        try:
            text = body.decode("utf-8", errors="replace").strip()
        except Exception:
            return False
        # Accept either the exact knarr form or a JSON body containing
        # both markers in any order / whitespace.
        if '"error"' in text and '"Unauthorized"' in text:
            return True
        return False

    async def _should_expose_sidecar(self) -> bool:
        """Decide whether to include the sidecar HiddenServicePort in torrc.

        Two gates: (1) operator-level expose_sidecar config, (2) HTTP auth probe.
        If probe fails, require explicit sidecar_allow_unauth=true to override.
        """
        cfg = self._tor_cfg()
        if not cfg.get("expose_sidecar", False):
            return False  # operator did not opt in

        auth_ok = await self._verify_sidecar_auth()
        if auth_ok:
            self._sidecar_auth_verified = True
            return True

        # Auth probe failed — check explicit override
        if cfg.get("sidecar_allow_unauth", False):
            self._emit(
                "tor.sidecar_unauth_allowed",
                reason="operator opted into exposing unauthed sidecar via sidecar_allow_unauth",
            )
            return True

        # Hard default: don't expose
        self._emit(
            "tor.sidecar_exposure_refused",
            reason="expose_sidecar=true but auth probe failed and sidecar_allow_unauth=false",
        )
        return False

    # ------------------------------------------------------------------
    # torrc generation
    # ------------------------------------------------------------------

    async def _generate_torrc(self) -> str:
        """Build the torrc.knarr fragment with optional sidecar exposure."""
        cfg = self._tor_cfg()
        data_dir = cfg.get("data_dir") or self._tor_data_dir or ""
        knarr_port = int(self._knarr_port)

        lines: List[str] = []
        if cfg.get("key_mode") == "shared":
            # Phase 6 O-14 fix: quote the data_dir path so whitespace or
            # special characters (rare on POSIX, common on Windows) don't
            # break the Tor parser. Validation in _validate_config already
            # rejected quotes / newlines in data_dir, so this is safe.
            lines.append(f'HiddenServiceDir "{data_dir}"')
        else:
            lines.append("# HiddenServiceDir — operator-managed (separate mode)")
        lines.append("HiddenServiceVersion 3")
        lines.append(f"HiddenServicePort {knarr_port} 127.0.0.1:{knarr_port}")

        if await self._should_expose_sidecar():
            lines.append(f"HiddenServicePort {self._sidecar_port} 127.0.0.1:{self._sidecar_port}")

        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # Shared-mode identity bridging
    # ------------------------------------------------------------------

    def _write_shared_identity(self, seed: bytes) -> Optional[str]:
        """Write Tor's hs_ed25519_secret_key from knarr identity seed.

        Expanded secret is SHA-512 of the 32-byte seed, prefixed with Tor's
        32-byte hs_ed25519v1 header. Only called in shared mode.
        """
        cfg = self._tor_cfg()
        data_dir = Path(cfg.get("data_dir") or self._tor_data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                data_dir.chmod(0o700)
            except OSError:
                pass

        try:
            from nacl.signing import SigningKey
        except ImportError:
            log.warning("TOR_SHARED_MODE_PYNACL_MISSING — cannot derive shared identity")
            return None

        signing_key = SigningKey(seed)
        pubkey = signing_key.verify_key.encode()
        # Phase 6 O-1 fix: apply Ed25519 bit-clamping per RFC 8032 §5.1.5 before
        # persisting. Tor reads the on-disk expanded secret with
        # `scalarmult_ed25519_base_noclamp`, so unclamped bytes produce a
        # pubkey that does NOT match the plugin's advertised onion
        # (nacl's SigningKey internally clamps before base-scalarmult).
        # Without this, shared mode publishes one onion but Tor serves a
        # different one, making the peer unreachable.
        # Ref: tor/src/lib/crypt_ops/crypto_ed25519.c ed25519_secret_key_from_seed
        expanded_ba = bytearray(hashlib.sha512(seed).digest())  # 64 bytes
        expanded_ba[0] &= 248
        expanded_ba[31] &= 127
        expanded_ba[31] |= 64
        expanded = bytes(expanded_ba)
        secret_path = data_dir / "hs_ed25519_secret_key"
        secret_path.write_bytes(_TOR_SECRET_HEADER + expanded)
        if os.name != "nt":
            try:
                secret_path.chmod(0o600)
            except OSError:
                pass

        self._self_pubkey = pubkey
        self._own_onion = self.onion_address_from_pubkey(pubkey)
        return self._own_onion

    # ------------------------------------------------------------------
    # Pool wiring
    # ------------------------------------------------------------------

    def _get_pool(self, ctx: Optional[PluginContext]) -> Any:
        """Resolve the ConnectionPool from the plugin context (defensive)."""
        if ctx is None:
            return None
        node = getattr(ctx, "_node", None)
        if node is not None:
            pool = getattr(node, "pool", None) or getattr(node, "_pool", None)
            if pool is not None:
                return pool
        return getattr(ctx, "pool", None) or getattr(ctx, "_pool", None)

    def _peer_pubkey_lookup(self, peer_id: str) -> Optional[str]:
        """Sync pubkey lookup for the pool's async refill.

        Called in run_in_executor by the pool (O-4 no-blocking-sqlite-on-hot-path).
        Uses storage.get_pubkey_by_node_id which exists in the clean tree at
        dht/storage.py:2405-2412. Returns hex-encoded pubkey or None.
        """
        ctx = self._ctx
        if ctx is None:
            return None
        node = getattr(ctx, "_node", None)
        if node is None:
            return None
        storage = getattr(node, "storage", None)
        if storage is None:
            return None
        fn = getattr(storage, "get_pubkey_by_node_id", None)
        if not callable(fn):
            return None
        try:
            return fn(peer_id)
        except Exception:
            return None

    def _configure_pool(self, cfg: Dict[str, Any]) -> None:
        """Wire TorPlugin into the ConnectionPool via the v1.1 seams."""
        pool = self._get_pool(self._ctx)
        if pool is None:
            return
        try:
            pool.set_tor_dialer(self._socks5_dialer)
            pool.set_tor_key_mode(cfg.get("key_mode", "separate"))
            pool.set_peer_pubkey_lookup(self._peer_pubkey_lookup)
            # Instantiate and attach the budget so the pool's _open can consult it.
            self._budget = _CircuitBudget(
                per_peer=int(cfg.get("max_circuits_per_peer_per_minute", 10)),
                global_=int(cfg.get("max_circuits_global_per_minute", 60)),
                collapse_aliases=bool(cfg.get("collapse_pubkey_aliases", True)),
            )
            pool.set_circuit_budget(self._budget)
            # Non-setter config passed as attributes (pool reads them directly)
            pool._prefer_onion = bool(cfg.get("prefer_onion", False))
            pool._derived_onion_fallback_enabled = bool(cfg.get("derived_onion_fallback", True))
            pool._derived_onion_fallback_window = float(cfg.get("derived_onion_fallback_window", 300))
            pool.set_bus(self._bus)
        except Exception as e:
            log.warning("TOR_POOL_SEAM_WIRE_FAIL err=%s", e)

    # ------------------------------------------------------------------
    # SOCKS5 dialer — registered on the pool via set_tor_dialer
    # ------------------------------------------------------------------

    async def _socks5_dialer(
        self,
        host: str,
        port: int,
        ssl_ctx: Any = None,
        peer_id: str = "",
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Dial host:port via SOCKS5 proxy using per-peer stream isolation."""
        if self._python_socks is None:
            raise ConnectionError("python-socks not available — Tor dialer disabled")

        cfg = self._tor_cfg()
        socks_host = cfg.get("socks_host", "127.0.0.1")
        socks_port = int(cfg.get("socks_port", 9050))

        # Per-peer stream isolation via SOCKS5 username/password (different
        # creds → Tor builds different circuits). "global" omits creds so all
        # peers share the default circuit.
        username = password = None
        if cfg.get("circuit_sharing", "per_peer") == "per_peer" and peer_id:
            username = peer_id[:255]
            password = "knarr"

        started = time.monotonic()
        # Phase 6 O-8 fix: compose the cache key from the triple directly,
        # not from an f-string URL. `Proxy.from_url` URL-parses the authority
        # section, which misparses peer_ids containing URL-reserved characters
        # (`:`, `@`, `/`, `?`, `#`, whitespace, `%`). Using a tuple key +
        # explicit-credentials constructor bypasses the parser entirely and
        # is also cheaper per-dial.
        cache_key = (socks_host, socks_port, username or "")
        try:
            # Proxy cache lookup — each (host, port, username) triple gets
            # its own Proxy. Per-peer stream isolation is preserved because
            # each peer_id produces a distinct username.
            proxy = self._proxy_cache.get(cache_key)
            if proxy is None:
                # Phase 6 O-8: explicit-credentials API instead of from_url.
                from python_socks.async_.asyncio import Proxy  # type: ignore
                from python_socks import ProxyType  # type: ignore
                proxy = Proxy(
                    proxy_type=ProxyType.SOCKS5,
                    host=socks_host,
                    port=socks_port,
                    username=username,
                    password=password,
                    rdns=True,
                )
                # Phase 6 O-5 fix: FIFO cap on the proxy cache
                if len(self._proxy_cache) >= self._proxy_cache_max:
                    try:
                        self._proxy_cache.pop(next(iter(self._proxy_cache)))
                    except StopIteration:
                        pass
                self._proxy_cache[cache_key] = proxy
            sock = await proxy.connect(dest_host=host, dest_port=int(port), timeout=30)
        except Exception as e:
            # Invalidate the cached Proxy on dial failure — it may be in a bad
            # state. The next dial for the same key will reconstruct.
            self._proxy_cache.pop(cache_key, None)
            self._emit("tor.circuit_failed", peer_id=peer_id, onion_address=host,
                       error=str(e), phase="socks_connect")
            raise

        # Phase 6 O-6 fix: wrap `open_connection` in its own try/except so a
        # TLS handshake failure (or any other mid-handshake error) does not
        # orphan the raw sock returned by proxy.connect. Without this, the
        # sock descriptor leaks until GC, and operators lose the
        # distinct-phase diagnostic signal.
        try:
            reader, writer = await asyncio.open_connection(
                sock=sock,
                ssl=ssl_ctx,
                server_hostname=host if ssl_ctx is not None else None,
            )
        except Exception as e:
            try:
                sock.close()
            except Exception:
                pass
            self._emit(
                "tor.circuit_failed",
                peer_id=peer_id,
                onion_address=host,
                error=str(e),
                phase="tls_handshake",
            )
            raise
        self._circuit_count += 1
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if elapsed_ms > float(cfg.get("slow_circuit_warning_ms", 5000)):
            self._emit(
                "tor.circuit_slow",
                peer_id=peer_id,
                onion_address=host,
                elapsed_ms=elapsed_ms,
            )
        return reader, writer

    # ------------------------------------------------------------------
    # Plugin lifecycle
    # ------------------------------------------------------------------

    async def on_init(self, ctx: Optional[PluginContext] = None) -> None:
        """Plugin initialization — called by the PluginLoader (or the interim
        on_load hook until PluginLoader.on_init dispatch lands).
        """
        if ctx is not None:
            self._ctx = ctx
            self._bus = getattr(ctx, "bus", None)

        self._validate_config()
        cfg = self._tor_cfg()
        if not cfg.get("enabled", False):
            log.info("TOR_INIT_SKIP enabled=false")
            return

        # Discover ports from the DHTNode
        node = getattr(self._ctx, "_node", None) if self._ctx is not None else None
        if node is not None:
            node_info = getattr(node, "node_info", None)
            if node_info is not None:
                self._knarr_port = int(getattr(node_info, "port", 9010))
            self._sidecar_port = int(getattr(node, "_sidecar_port", 9031))
        self._tor_data_dir = cfg.get("data_dir") or self._default_data_dir()

        # 1. Lazy-import python-socks (O-11 — optional extra)
        try:
            self._python_socks = importlib.import_module("python_socks.async_.asyncio")
            self._dependency_loaded = True
        except ImportError as e:
            self._emit("tor.dependency_missing", module="python_socks", error=str(e))
            log.warning("TOR_DEPENDENCY_MISSING python_socks err=%s", e)
            self._dependency_loaded = False
            return  # no-op plugin

        # 2. Control port handshake (§2.2.1 + O-6)
        self._control = AsyncControlPort(
            host=cfg.get("socks_host", "127.0.0.1"),
            port=int(cfg.get("control_port", 9051)),
            auth_method=cfg.get("control_auth_method", "cookie"),
            cookie_file=cfg.get("control_cookie_file", ""),
            password=cfg.get("control_password", ""),
            bus_emit=self._emit,
            slow_circuit_warning_ms=int(cfg.get("slow_circuit_warning_ms", 5000)),
        )
        try:
            await self._control.connect()
            info = await self._control.protocol_info()
            self._tor_version = info.get("version", "")
            self._daemon_reachable = True
            self._last_consensus_check = time.time()
            self._emit(
                "tor.daemon_reachable",
                socks_port=int(cfg.get("socks_port", 9050)),
                control_port=int(cfg.get("control_port", 9051)),
                version=self._tor_version,
            )
        except ControlPortError as e:
            self._emit("tor.daemon_not_tor", socks_port=int(cfg.get("socks_port", 9050)), error=str(e))
            self._daemon_reachable = False
            await self._control.disconnect()
            return
        except (TimeoutError, ConnectionError, OSError) as e:
            self._emit("tor.daemon_unreachable", socks_port=int(cfg.get("socks_port", 9050)), error=str(e))
            self._daemon_reachable = False
            await self._control.disconnect()
            return

        # 3. Authenticate + subscribe to events
        try:
            await self._control.authenticate()
            self._control_auth_ok = True
            await self._control.subscribe_events()
            # Phase 6 O-3 fix: use start_event_loop() so _event_loop_active
            # flips to True BEFORE the background task is scheduled. This
            # guarantees any subsequent _send_command goes through the demux
            # path (future-based) instead of racing on the reader.
            self._control_task = self._control.start_event_loop()
        except Exception as e:
            self._emit(
                "tor.control_port_unavailable",
                control_port=int(cfg.get("control_port", 9051)),
                error=str(e),
            )
            self._control_auth_ok = False
            # Continue — SOCKS-only observability mode

        # 4. Wire the pool seams
        self._configure_pool(cfg)

        # 5. Key-mode setup + compute own onion
        if cfg.get("key_mode") == "shared":
            seed = self._get_identity_seed()
            if seed is None:
                log.warning("TOR_SHARED_MODE_SEED_UNAVAILABLE — falling back to separate mode behavior")
            else:
                self._write_shared_identity(seed)
        else:
            # Separate mode — plugin doesn't write into Tor data dir
            data_dir = Path(self._tor_data_dir)
            if data_dir.is_dir():
                hostname = data_dir / "hostname"
                if hostname.is_file():
                    try:
                        val = hostname.read_text(encoding="utf-8").strip().split()[0]
                        if val.endswith(".onion"):
                            self._own_onion = val
                    except (OSError, IndexError):
                        pass

        # 6. torrc fragment
        # Phase 6 G-5 fix: write the fragment in BOTH shared and separate
        # mode. In separate mode the operator runs Tor externally — they
        # still benefit from a reference file the plugin emits alongside
        # the Tor data dir, documenting exactly the HiddenServiceDir /
        # HiddenServicePort lines knarr expects.
        try:
            torrc = await self._generate_torrc()
            data_dir = Path(self._tor_data_dir)
            if data_dir.is_dir():
                target = data_dir / "torrc.knarr"
                try:
                    target.write_text(torrc, encoding="utf-8")
                    self._emit(
                        "tor.torrc_written",
                        path=str(target),
                        key_mode=cfg.get("key_mode", "separate"),
                    )
                except OSError as write_err:
                    log.warning("TOR_TORRC_WRITE_FAIL path=%s err=%s",
                                target, write_err)
        except Exception as e:
            log.warning("TOR_TORRC_GENERATE_FAIL err=%s", e)

        # 7. Runtime advertise_host override (O-13 — dataclass replacement)
        self._maybe_override_advertise_host(cfg)

        # 8. Phase 6 G-5 fix: in separate mode, the `hostname` file is written
        # by Tor asynchronously after it publishes the hidden service. On
        # first-run (fresh data dir), the file does not exist when on_init
        # runs — `_own_onion` stays None, `_maybe_override_advertise_host`
        # no-ops, and `_verify_hidden_service` short-circuits. Without this
        # polling task, the operator has to restart knarr after Tor writes
        # the file. The poll task retries every 10s for up to 10 minutes
        # until the hostname appears, at which point it re-runs the override
        # and verification.
        if cfg.get("key_mode") != "shared" and self._own_onion is None:
            self._separate_mode_poll_task = asyncio.create_task(
                self._poll_separate_mode_hostname(cfg)
            )

        # 9. Schedule hidden service verification (best-effort)
        self._self_dial_task = asyncio.create_task(self._verify_hidden_service())

    async def _poll_separate_mode_hostname(self, cfg: Dict[str, Any]) -> None:
        """Phase 6 G-5: poll the Tor data_dir/hostname file until it appears.

        Separate mode's torrc is owned by the operator — Tor writes
        `hostname` after it publishes the hidden service descriptor, which
        can take seconds to minutes depending on network reachability. The
        plugin must not require a knarr restart to pick it up.

        Polls every 10s for up to 10 minutes. On success, re-runs
        ``_maybe_override_advertise_host`` and restarts
        ``_verify_hidden_service`` so the cockpit flips green without
        operator intervention. Exits quietly on timeout (operator should
        check Tor logs).
        """
        max_attempts = 60  # 60 * 10s = 10 minutes
        interval = 10.0
        data_dir_path = Path(self._tor_data_dir) if self._tor_data_dir else None
        if data_dir_path is None:
            return

        for attempt in range(max_attempts):
            await asyncio.sleep(interval)
            if self._own_onion is not None:
                return  # already discovered elsewhere
            hostname = data_dir_path / "hostname"
            if not hostname.is_file():
                continue
            try:
                value = hostname.read_text(encoding="utf-8").strip().split()[0]
            except (OSError, IndexError):
                continue
            if not value.endswith(".onion"):
                continue
            self._own_onion = value
            self._emit(
                "tor.hostname_discovered",
                onion_address=value,
                attempts=attempt + 1,
            )
            log.info("TOR_HOSTNAME_DISCOVERED onion=%s after %d polls",
                     value, attempt + 1)
            # Retry the advertise_host override now that we know the onion
            try:
                self._maybe_override_advertise_host(cfg)
            except Exception as e:
                log.warning("TOR_OVERRIDE_RETRY_FAIL err=%s", e)
            # Restart hidden service verification
            try:
                if (self._self_dial_task is None
                        or self._self_dial_task.done()):
                    self._self_dial_task = asyncio.create_task(
                        self._verify_hidden_service()
                    )
            except Exception as e:
                log.warning("TOR_VERIFY_RESTART_FAIL err=%s", e)
            return
        # Timeout — operator must investigate Tor-side
        self._emit(
            "tor.hostname_poll_timeout",
            data_dir=str(data_dir_path),
            attempts=max_attempts,
        )
        log.warning(
            "TOR_HOSTNAME_POLL_TIMEOUT data_dir=%s — check Tor logs / "
            "torrc.knarr for the HiddenServiceDir path", data_dir_path
        )

    def _default_data_dir(self) -> str:
        """OS-appropriate default Tor data dir when config doesn't specify one."""
        if os.name == "nt":
            return "F:/knarr.node/.node/tor"
        xdg = os.environ.get("XDG_DATA_HOME")
        root = Path(xdg) if xdg else Path.home() / ".local" / "share"
        return str(root / "knarr" / "tor")

    def _get_identity_seed(self) -> Optional[bytes]:
        """Load the node's Ed25519 identity seed for shared-mode bridging."""
        ctx = self._ctx
        if ctx is None:
            return None
        vault_get = getattr(ctx, "vault_get", None)
        if callable(vault_get):
            for key in ("identity_seed", "node_seed"):
                try:
                    seed = vault_get(key)
                except Exception:
                    seed = None
                if isinstance(seed, (bytes, bytearray)) and len(seed) == 32:
                    return bytes(seed)
                if isinstance(seed, str):
                    s = seed.strip()
                    if len(s) == 64:
                        try:
                            return bytes.fromhex(s)
                        except ValueError:
                            continue
        return None

    def _maybe_override_advertise_host(self, cfg: Dict[str, Any]) -> None:
        """Runtime-only override of node_info.host via dataclass replacement.

        GPT's contribution: NodeInfo is @dataclass(frozen=True) AND has no
        advertise_host field, so direct attribute assignment (what Opus and
        Sonnet both tried) would raise FrozenInstanceError. Dataclass
        replacement is the correct approach.

        NEVER modifies knarr.toml — the override is only in memory, so
        disabling Tor via config + restart cleanly restores the pre-Tor host.
        """
        if not cfg.get("advertise_onion", True):
            return
        own_onion = self.get_own_onion_address()
        if not own_onion:
            return

        node = getattr(self._ctx, "_node", None) if self._ctx is not None else None
        if node is None or not hasattr(node, "node_info"):
            return
        current = node.node_info
        previous_host = getattr(current, "host", "")
        if previous_host == own_onion:
            # Phase 6 O-16 fix: already set — still emit the bus event so
            # cooperative consumers (cockpit, downstream plugins) see the
            # signal on every init / reload. Without this, a plugin reload
            # would leave the cockpit without the post-reload announcement.
            self._emit(
                "tor.advertise_host_overridden",
                onion=own_onion,
                previous=previous_host,
                reapplied=True,
            )
            return
        try:
            # B-05: snapshot the pre-override host once, on first override.
            # Skip if already populated so re-override (after a bus-event
            # reapplied branch) doesn't overwrite the true original with an
            # onion.
            if self._pre_override_host is None:
                self._pre_override_host = previous_host
            node.node_info = NodeInfo(
                node_id=current.node_id,
                host=own_onion,
                port=current.port,
            )
            self._emit(
                "tor.advertise_host_overridden",
                onion=own_onion,
                previous=previous_host,
                reapplied=False,
            )
            log.info("TOR_ADVERTISE_HOST onion=%s (runtime override)", own_onion)
        except Exception as e:
            log.debug("TOR_ADVERTISE_HOST_FAIL err=%s", e)

    async def _verify_hidden_service(self) -> None:
        """Best-effort verification of hidden service liveness (Opus's state machine).

        Path (a): control port GETINFO hs/service/status/{name} — first positive
        flips _hidden_service_published.
        Path (b): SOCKS5 self-dial to own onion — used as fallback at the final
        backoff iteration.

        Exponential backoff [2, 5, 10, 20, 30]s ≈ 67s total. Cockpit stays
        yellow until one path succeeds or the task ends.
        """
        if not self._daemon_reachable or not self._own_onion:
            return
        delays = [2.0, 5.0, 10.0, 20.0, 30.0]
        for delay in delays:
            await asyncio.sleep(delay)
            if self._hidden_service_published:
                return

            # Path (a) — control port GETINFO hs/service/status/{name}
            if self._control_auth_ok and self._control is not None:
                try:
                    name = self._own_onion.split(".", 1)[0]
                    status = await self._control.get_hs_status(name)
                    # Phase 6 G-4 / O-7 fix: removed "" from the accepted
                    # tuple. `get_hs_status` returns {"state": ""} when the
                    # GETINFO reply cannot be parsed (reply shape changed,
                    # parser missed the token). Treating that as "live" was
                    # a false positive — empty state must fall through to
                    # the self-dial path.
                    if status is not None and status.get("state") in (
                        "active", "ready", "published"
                    ):
                        self._hidden_service_published = True
                        self._last_hs_verify = time.time()
                        self._emit(
                            "tor.hidden_service_live",
                            onion_address=self._own_onion,
                            verification_method="control_port",
                        )
                        return
                except Exception:
                    pass

            # Path (b) — self-dial (only on final iteration, since a circuit costs 2-5s)
            if delay >= 30.0 and self._python_socks is not None:
                # Phase 6 G-4 fix: require TLS handshake completion on the
                # self-dial. Previous code passed ssl_ctx=None and accepted
                # a bare TCP connect as "live" — but clients of the node
                # always use TLS, so TCP-only reachability is a false
                # positive. Pull the real client TLS context from the node.
                tls_ctx = None
                node = getattr(self._ctx, "_node", None) if self._ctx is not None else None
                if node is not None:
                    tls_ctx = getattr(node, "_client_ssl_ctx", None)
                try:
                    reader, writer = await self._socks5_dialer(
                        host=self._own_onion,
                        port=self._knarr_port,
                        ssl_ctx=tls_ctx,
                        peer_id="__self_probe__",
                    )
                    # Verify that a TLS session actually established. If
                    # tls_ctx was None we accept the TCP connect as before
                    # (e.g. TLS-disabled dev nodes), but emit a warning.
                    tls_ok = True
                    if tls_ctx is not None:
                        ssl_obj = writer.get_extra_info("ssl_object")
                        tls_ok = ssl_obj is not None
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass
                    if not tls_ok:
                        self._emit(
                            "tor.hidden_service_tls_handshake_failed",
                            onion_address=self._own_onion,
                        )
                        continue  # don't flip readiness on tcp-only success
                    self._hidden_service_published = True
                    self._last_hs_verify = time.time()
                    self._emit(
                        "tor.hidden_service_live",
                        onion_address=self._own_onion,
                        verification_method="self_dial",
                    )
                    return
                except Exception:
                    pass

    async def on_shutdown(self) -> None:
        """Clean shutdown — cancel background tasks, close control port, clear proxy cache."""
        for task in (self._control_task, self._self_dial_task,
                     self._separate_mode_poll_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except Exception:
                    pass
        if self._control is not None:
            try:
                await self._control.disconnect()
            except Exception:
                pass
        # Clear proxy cache so a subsequent reload starts fresh (§8 bug #2).
        self._proxy_cache.clear()

        # B-05: restore advertise_host if we overrode it. Only revert when
        # node_info still shows the onion we set — if some other layer has
        # rewritten host since, leave it alone.
        if self._pre_override_host is not None:
            try:
                node = getattr(self._ctx, "_node", None) if self._ctx is not None else None
                if node is not None and hasattr(node, "node_info"):
                    current = node.node_info
                    if getattr(current, "host", "") == self._own_onion:
                        node.node_info = NodeInfo(
                            node_id=current.node_id,
                            host=self._pre_override_host,
                            port=current.port,
                        )
                        log.info(
                            "TOR_ADVERTISE_HOST_RESTORED host=%s",
                            self._pre_override_host,
                        )
            except Exception as e:
                log.debug("TOR_ADVERTISE_HOST_RESTORE_FAIL err=%s", e)
            self._pre_override_host = None
