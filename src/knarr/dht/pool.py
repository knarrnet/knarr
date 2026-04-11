"""Connection pool with Tor transport seams — SPEC-tor-plugin.md v1.1.

Synthesized additions to the clean-tree pool (feature/tor-plugin branch):
- GPT: _conn_hosts host-aware connection reuse (caught the reuse-across-transports
  concurrency bug that Opus and Sonnet missed)
- Opus: _handle_dial_failure fallback handler structure, _CircuitBudget seam
  (class lives in TorPlugin, pool calls set_circuit_budget(...) to attach)
- Sonnet: pre-Tor peer skip pattern at top of _open (cleanest ordering)
- GPT + Opus merge: _resolve_transport_host cache-only with _normalize_pubkey helper

Full rationale: F:\\thing\\specs\\SYNTHESIS-tor-plugin.md §1 component picks.
"""
import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from ..core.messages import Message
from ..core.crypto import get_tls_peer_cert_fingerprint
from .protocol import send_message, receive_message

logger = logging.getLogger(__name__)

_SEND_FAILED = object()  # sentinel for failed send attempts


class ConnectionPool:
    """Persistent TCP connections to known peers. LIFO eviction.

    All operations on a given peer are serialized by a per-peer asyncio.Lock.
    The lock covers check/create/send/receive so there is no TOCTOU window
    for duplicate connections or interleaved stream writes.

    Tor plugin v1.1 seams (spec §2.1 + §2.5):
      - ``set_tor_dialer(dialer)`` — register the SOCKS5 dialer for .onion targets.
      - ``get_tor_dialer()`` — introspection: "is Tor registered?"
      - ``set_tor_key_mode(mode)`` — gate dual-stack auto-resolution ("shared"
        enables derivation; "separate" disables it per F-1).
      - ``set_peer_pubkey_lookup(fn)`` — register the sync callable for async
        cache refill. NEVER called on the hot path (O-4 invariant).
      - ``set_circuit_budget(budget)`` — attach the TorPlugin's _CircuitBudget
        instance. Pool calls budget.allow() before every onion dial.
      - ``set_prefer_onion(bool)`` — consumer-side dual-stack preference.
      - ``set_bus(bus)`` — inject the bus for emitting tor.* events from pool.
      - ``_resolve_transport_host(peer_id, host)`` — synchronous host
        resolution. Cache-only, no I/O. Returns the transport-target host.
      - ``_conn_hosts`` — per-peer_id record of the host used when the pooled
        connection was opened. Used to detect "same peer_id, different host"
        and force a clean reconnect (GPT's correctness guard).
    """

    def __init__(self, max_connections: int = 50):
        self._pool: Dict[str, Tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._lock_guard = asyncio.Lock()  # protects _locks dict creation
        self._max = max_connections
        self._last_used: Dict[str, float] = {}
        self._creation_order: List[str] = []  # newest last — LIFO evicts from end
        self._tls_ctx = None        # C-02: client TLS context
        self._tls_required = True   # C-02: reject plaintext when True

        # ------------------------------------------------------------------
        # Tor plugin seams (spec §2.1 + §2.5, v1.1)
        # ------------------------------------------------------------------

        # Host-aware connection reuse (GPT contribution). Records the host
        # used when the connection for peer_id was opened. When the pool is
        # asked to send to the same peer_id with a different host (clearnet
        # → onion or onion → clearnet via dual-stack resolution), the cached
        # connection is closed first to prevent wrong-transport reuse.
        self._conn_hosts: Dict[str, str] = {}

        # Dialer callable(host, port, ssl_ctx, peer_id=...) -> (reader, writer).
        # None until TorPlugin.on_init registers it via set_tor_dialer.
        self._tor_dialer: Optional[Callable] = None

        # Local key mode — gates dual-stack auto-resolution per F-1.
        self._tor_key_mode: str = "separate"

        # Dual-stack preference — only takes effect in shared mode.
        self._prefer_onion: bool = False

        # Pubkey cache (sync read on hot path, async refill off-path).
        self._peer_pubkey_cache: Dict[str, bytes] = {}
        self._peer_pubkey_cache_max: int = max_connections * 2

        # Synchronous lookup callable — only invoked via run_in_executor.
        self._peer_pubkey_lookup: Optional[Callable[[str], Optional[str]]] = None

        # Circuit budget instance (TorPlugin's _CircuitBudget class).
        # When None, no rate-limiting is enforced at the pool layer.
        self._circuit_budget: Any = None

        # Derived-onion fallback state (§2.5 F-2 + O-5).
        self._derived_onion_fallback_enabled: bool = True
        self._derived_onion_fallback_window: float = 300.0
        self._derived_onion_fallback_ts: Dict[str, float] = {}

        # Pool-level bus emit (injected by node startup; None tolerated).
        self._bus: Any = None

        # Pre-Tor peer skip state (§9.3, O-10) — log once per peer.
        self._pre_tor_skip_logged: set = set()

    def set_tls_context(self, ctx, tls_required: bool = True):
        """C-02: Set TLS context for outbound connections."""
        self._tls_ctx = ctx
        self._tls_required = tls_required

    # ------------------------------------------------------------------
    # Tor plugin seams (§3.2 — v1.1 lifecycle per O-14)
    # ------------------------------------------------------------------

    def set_tor_dialer(self, dialer: Optional[Callable]) -> None:
        """Register (or clear) the SOCKS5 dialer for .onion targets.

        Lifecycle: the second call REPLACES the first — no chain. Passing
        None unregisters. Plugin reload requires a node restart. During the
        window between ``__init__`` and ``TorPlugin.on_init``, ``_tor_dialer``
        is None so .onion hosts fall through to the pre-Tor-peer skip path.
        """
        self._tor_dialer = dialer

    def get_tor_dialer(self) -> Optional[Callable]:
        """Return the currently registered Tor dialer, or None if unregistered."""
        return self._tor_dialer

    def set_tor_key_mode(self, mode: str) -> None:
        """Register the local key mode ('shared' or 'separate')."""
        if mode not in ("shared", "separate"):
            raise ValueError(f"tor key mode must be 'shared' or 'separate', got {mode!r}")
        self._tor_key_mode = mode

    def set_peer_pubkey_lookup(self, lookup: Optional[Callable]) -> None:
        """Register the peer pubkey lookup callback for dual-stack resolution.

        The callable must be synchronous. It is ONLY invoked via
        ``run_in_executor`` in ``_refill_peer_pubkey_async`` — never on the
        hot path. This is the O-4 invariant: no blocking SQLite on ``send``.
        """
        self._peer_pubkey_lookup = lookup

    def set_circuit_budget(self, budget: Any) -> None:
        """Attach the TorPlugin's _CircuitBudget instance.

        The budget exposes ``allow(peer_id, pubkey_hex, now) -> (bool, reason)``.
        The pool calls it from ``_open`` before every Tor-routed dial.
        """
        self._circuit_budget = budget

    def set_prefer_onion(self, prefer: bool) -> None:
        """Toggle dual-stack preference (shared mode only — §2.5 gating)."""
        self._prefer_onion = bool(prefer)

    def set_bus(self, bus: Any) -> None:
        """Inject the bus emit target. None is tolerated (no-op emits)."""
        self._bus = bus

    # ------------------------------------------------------------------
    # Bus emit helper — tolerant of missing bus
    # ------------------------------------------------------------------

    def _emit(self, event_type: str, **fields: Any) -> None:
        """Emit a bus event from the pool. None bus is a silent no-op."""
        bus = self._bus
        if bus is None:
            return
        try:
            bus.emit(event_type, **fields)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helpers (case-insensitive onion check, pubkey normalization)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_onion_host(host: str) -> bool:
        """Case-insensitive .onion suffix check."""
        return bool(host) and host.lower().endswith(".onion")

    @staticmethod
    def _normalize_pubkey(value: Any) -> Optional[bytes]:
        """Normalize various pubkey representations to 32 raw bytes.

        GPT contribution — defensive helper that accepts both bytes and hex
        string inputs and validates length. Returns None on any parse failure.
        """
        if isinstance(value, (bytes, bytearray)):
            return bytes(value) if len(value) == 32 else None
        if isinstance(value, str):
            value = value.strip()
            if len(value) != 64:
                return None
            try:
                return bytes.fromhex(value)
            except ValueError:
                return None
        return None

    # ------------------------------------------------------------------
    # Dual-stack resolution (§2.5 — v1.1 shared-only per F-1 + O-4 cache)
    # ------------------------------------------------------------------

    def _resolve_transport_host(self, peer_id: str, host: str) -> str:
        """Return the transport-target host for this dial.

        Decision tree (cache-only, NO I/O):
          1. No tor dialer registered → return ``host`` unchanged.
          2. prefer_onion disabled → return ``host`` unchanged.
          3. key_mode != "shared" → F-1: return ``host`` unchanged
             (separate mode cannot derive peer onions from pubkeys).
          4. host already ends in ``.onion`` → return unchanged (pass-through).
          5. Pubkey cache miss → return ``host`` unchanged; fire async refill
             for next time. Preserves the O-4 invariant: no blocking SQLite.
          6. Pubkey cache hit → derive onion via ``onion_address_from_pubkey``.
        """
        if self._tor_dialer is None:
            return host
        if not self._prefer_onion:
            return host
        if self._tor_key_mode != "shared":
            return host  # F-1: separate mode cannot derive
        if self._is_onion_host(host):
            return host

        pubkey = self._peer_pubkey_cache.get(peer_id)
        pubkey = self._normalize_pubkey(pubkey)
        if pubkey is None:
            # Cache miss → dial clearnet now, refill async for next time.
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._refill_peer_pubkey_async(peer_id))
            except RuntimeError:
                pass
            return host

        # Derive onion — import locally to avoid a circular dep on module load
        # when the plugin isn't installed.
        try:
            from ..plugins.tor.handler import onion_address_from_pubkey
            return onion_address_from_pubkey(pubkey)
        except Exception:
            return host

    async def _refill_peer_pubkey_async(self, peer_id: str) -> None:
        """Fire-and-forget async refill of the pubkey cache via run_in_executor."""
        if peer_id in self._peer_pubkey_cache:
            return
        lookup = self._peer_pubkey_lookup
        if lookup is None:
            return
        try:
            loop = asyncio.get_running_loop()
            pub_hex = await loop.run_in_executor(None, lookup, peer_id)
        except Exception:
            return
        if not pub_hex:
            return
        pub = self._normalize_pubkey(pub_hex)
        if pub is None:
            return
        # Simple FIFO cap
        if len(self._peer_pubkey_cache) >= self._peer_pubkey_cache_max:
            try:
                self._peer_pubkey_cache.pop(next(iter(self._peer_pubkey_cache)))
            except StopIteration:
                pass
        self._peer_pubkey_cache[peer_id] = pub

    # ------------------------------------------------------------------
    # Lock management
    # ------------------------------------------------------------------

    async def _get_lock(self, peer_id: str) -> asyncio.Lock:
        """Return (or create) the per-peer lock. Thread-safe via _lock_guard."""
        if peer_id not in self._locks:
            async with self._lock_guard:
                if peer_id not in self._locks:
                    self._locks[peer_id] = asyncio.Lock()
        return self._locks[peer_id]

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_healthy(conn: Tuple[asyncio.StreamReader, asyncio.StreamWriter]) -> bool:
        """Quick pre-flight check on a cached connection."""
        reader, writer = conn
        if writer.is_closing():
            return False
        if reader.at_eof():
            return False
        return True

    def _pubkey_hex_for(self, peer_id: str) -> Optional[str]:
        """Return the hex-encoded pubkey for a peer_id if cached, else None.

        Used by the circuit budget check to collapse Sybil aliases to pubkey
        when shared mode is active (spec §3 pattern rule).
        """
        pub = self._peer_pubkey_cache.get(peer_id)
        pub = self._normalize_pubkey(pub)
        if pub is None:
            return None
        return pub.hex()

    async def _open(self, peer_id: str, host: str, port: int,
                    timeout: float) -> Optional[Tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
        """Create a TCP connection and store it in the pool. Returns the pair or None.

        Tor plugin v1.1 changes:
          - Pre-Tor peer skip (§9.3, O-10): if host is .onion and no dialer
            is registered, log once per peer and return None without attempting
            ``asyncio.open_connection`` (which would fail with getaddrinfo and
            spam stack traces per call).
          - Circuit budget enforcement (§5.5 F-4): if this is a Tor-routed
            dial and ``_circuit_budget`` is attached, check ``allow()`` and
            emit ``tor.circuit_rate_limited`` on denial. Pubkey collapse is
            applied when a pubkey is cached for the peer.
          - Host-aware connection reuse tracking: on success, record the
            host used so subsequent reuse can detect transport changes.
        """
        if len(self._pool) >= self._max:
            self._evict_lifo()

        is_onion = self._is_onion_host(host)

        # Pre-Tor peer skip (O-10 §9.3) — peer advertises .onion but we don't
        # have Tor. Log once per peer, emit bus event once, return None.
        if is_onion and self._tor_dialer is None:
            if peer_id not in self._pre_tor_skip_logged:
                self._pre_tor_skip_logged.add(peer_id)
                logger.info(
                    "TOR_PEER_SKIPPED peer_id=%s host=%s (Tor not enabled locally)",
                    peer_id, host,
                )
                self._emit("tor.peer_is_onion_only_skipped", peer_id=peer_id, host=host)
            return None

        # Circuit budget enforcement (§5.5 F-4). Only applies to Tor-routed dials.
        use_tor = is_onion and self._tor_dialer is not None
        if use_tor and self._circuit_budget is not None:
            try:
                pubkey_hex = self._pubkey_hex_for(peer_id)
                allowed, reason = self._circuit_budget.allow(peer_id, pubkey_hex=pubkey_hex)
            except Exception:
                allowed, reason = True, ""  # budget errors never block a dial
            if not allowed:
                self._emit("tor.circuit_rate_limited", peer_id=peer_id, reason=reason)
                return None

        # C-02: Attempt TLS connection; fall back to plaintext if not required.
        ssl_ctx = self._tls_ctx
        try:
            if use_tor:
                reader, writer = await asyncio.wait_for(
                    self._tor_dialer(host, port, ssl_ctx, peer_id=peer_id),
                    timeout=timeout,
                )
            else:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, ssl=ssl_ctx), timeout=timeout
                )
        except Exception:
            if ssl_ctx is not None and not self._tls_required and not is_onion:
                # Plaintext fallback — clearnet only, onion paths always require TLS
                logger.warning(
                    "Pool TLS connect failed, falling back to plaintext host=%s port=%d",
                    host, port,
                )
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(host, port), timeout=timeout
                    )
                except Exception:
                    return None
            else:
                return None

        self._pool[peer_id] = (reader, writer)
        self._conn_hosts[peer_id] = host
        self._last_used[peer_id] = time.monotonic()
        self._creation_order.append(peer_id)
        return (reader, writer)

    async def _close_conn(self, peer_id: str):
        """Close a connection and remove from pool. Does NOT remove the lock."""
        if peer_id in self._pool:
            _, writer = self._pool.pop(peer_id)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            self._last_used.pop(peer_id, None)
            self._conn_hosts.pop(peer_id, None)
            if peer_id in self._creation_order:
                self._creation_order.remove(peer_id)

    async def _try_send(self, reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter, msg: Message,
                        timeout: float, host: str, port: int) -> object:
        """Send + receive on an existing connection. Returns response or _SEND_FAILED."""
        try:
            await send_message(writer, msg)
            response = await asyncio.wait_for(receive_message(reader), timeout=timeout)
            if response is None:
                return _SEND_FAILED
            object.__setattr__(
                response,
                "_tls_peer_cert_fingerprint",
                get_tls_peer_cert_fingerprint(writer.get_extra_info("ssl_object")),
            )
            object.__setattr__(response, "_tls_peer_host", host)
            object.__setattr__(response, "_tls_peer_port", int(port))
            return response
        except Exception:
            return _SEND_FAILED

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send(self, peer_id: str, host: str, port: int, msg: Message,
                   timeout: float = 10.0) -> Optional[Message]:
        """Send message on pooled connection. Creates if needed. Returns response.

        The entire check-create-send-receive flow runs under the per-peer lock,
        eliminating TOCTOU races that caused stream corruption in v0.14.0.

        Tor plugin v1.1 flow:
          1. Classify ``host`` as operator-explicit-onion (if literally .onion
             on entry) vs clearnet BEFORE acquiring the lock.
          2. ``_resolve_transport_host`` picks the actual dial target (clearnet
             or derived onion depending on shared-mode gating and cache state).
          3. If a cached connection exists for peer_id but was opened to a
             DIFFERENT host (clearnet vs onion), close it before reuse — GPT's
             host-aware connection reuse correctness guard.
          4. On dial failure, ``_handle_dial_failure`` applies the §2.5 F-2+O-5
             fallback policy: operator-explicit onion fails closed, derived
             onion retries clearnet once within a per-peer rate-limit window.
        """
        # Classify the original host BEFORE resolution — drives fallback policy.
        operator_explicit_onion = self._is_onion_host(host)
        transport_host = self._resolve_transport_host(peer_id, host)
        derived_onion_used = (
            not operator_explicit_onion
            and self._is_onion_host(transport_host)
            and transport_host != host
        )

        lock = await self._get_lock(peer_id)
        async with lock:
            # 1. Try existing connection — but verify the cached host matches.
            # GPT's host-aware reuse guard: if the cached connection was
            # opened to a different host than the one we want to dial now,
            # close it before reuse to prevent wrong-transport stream mixing.
            conn = self._pool.get(peer_id)
            if conn is not None:
                cached_host = self._conn_hosts.get(peer_id)
                if cached_host != transport_host:
                    await self._close_conn(peer_id)
                    conn = None
                elif not self._is_healthy(conn):
                    await self._close_conn(peer_id)
                    conn = None

            if conn is not None:
                result = await self._try_send(conn[0], conn[1], msg, timeout, transport_host, port)
                if result is not _SEND_FAILED:
                    self._last_used[peer_id] = time.monotonic()
                    return result
                # Broken — close and fall through to fresh connection
                await self._close_conn(peer_id)

            # 2. Fresh connection + send (first attempt, using resolved host)
            conn = await self._open(peer_id, transport_host, port, timeout)
            if conn is None:
                return await self._handle_dial_failure(
                    peer_id, host, transport_host, port, msg, timeout,
                    operator_explicit_onion, derived_onion_used,
                    reason="open_failed",
                )

            result = await self._try_send(conn[0], conn[1], msg, timeout, transport_host, port)
            if result is not _SEND_FAILED:
                self._last_used[peer_id] = time.monotonic()
                return result

            # 3. Retry once with brand new connection (same resolved host)
            await self._close_conn(peer_id)
            conn = await self._open(peer_id, transport_host, port, timeout)
            if conn is None:
                return await self._handle_dial_failure(
                    peer_id, host, transport_host, port, msg, timeout,
                    operator_explicit_onion, derived_onion_used,
                    reason="retry_open_failed",
                )

            result = await self._try_send(conn[0], conn[1], msg, timeout, transport_host, port)
            if result is not _SEND_FAILED:
                self._last_used[peer_id] = time.monotonic()
                return result

            await self._close_conn(peer_id)
            return await self._handle_dial_failure(
                peer_id, host, transport_host, port, msg, timeout,
                operator_explicit_onion, derived_onion_used,
                reason="send_failed",
            )

    async def _handle_dial_failure(
        self,
        peer_id: str,
        original_host: str,
        transport_host: str,
        port: int,
        msg: Message,
        timeout: float,
        operator_explicit_onion: bool,
        derived_onion_used: bool,
        reason: str,
    ) -> Optional[Message]:
        """Apply the derived-onion fallback policy (§2.5 F-2 + O-5).

        Operator-explicit onion: fail closed, emit ``tor.operator_explicit_onion_failed``.
        Derived onion: retry clearnet once within the per-peer rate-limit window,
        emit ``tor.derived_onion_fallback``.

        Pubkey-collapse for fallback rate-limit: when shared mode is active
        and a pubkey is cached for the peer, the rate-limit dict is keyed by
        pubkey_hex instead of peer_id. This fixes Opus's self-flagged Sybil
        alias gap from SUBMISSION-NOTES §5.3 (synthesis audit §3).
        """
        if operator_explicit_onion:
            self._emit(
                "tor.operator_explicit_onion_failed",
                peer_id=peer_id, onion=original_host, reason=reason,
            )
            return None

        if not derived_onion_used or not self._derived_onion_fallback_enabled:
            return None

        # Synthesis audit §3: pubkey-collapse the rate-limit key when shared mode
        # is active and we know the peer's pubkey. Prevents Sybil aliases from
        # each getting their own fallback window.
        rl_key = peer_id
        if self._tor_key_mode == "shared":
            pubkey_hex = self._pubkey_hex_for(peer_id)
            if pubkey_hex:
                rl_key = pubkey_hex

        now = time.monotonic()
        last = self._derived_onion_fallback_ts.get(rl_key, 0.0)
        if now - last < self._derived_onion_fallback_window:
            self._emit(
                "tor.derived_onion_fallback_rate_limited",
                peer_id=peer_id,
                derived_onion=transport_host,
            )
            return None
        self._derived_onion_fallback_ts[rl_key] = now

        self._emit(
            "tor.derived_onion_fallback",
            peer_id=peer_id,
            derived_onion=transport_host,
            reason=reason,
            fallback_host=original_host,
        )

        # One retry on clearnet with the original host.
        conn = await self._open(peer_id, original_host, port, timeout)
        if conn is None:
            return None
        result = await self._try_send(conn[0], conn[1], msg, timeout, original_host, port)
        if result is _SEND_FAILED:
            await self._close_conn(peer_id)
            return None
        self._last_used[peer_id] = time.monotonic()
        return result

    # ------------------------------------------------------------------
    # Eviction
    # ------------------------------------------------------------------

    def _evict_lifo(self):
        """Evict the most recently added idle connection (LIFO)."""
        for i in range(len(self._creation_order) - 1, -1, -1):
            peer_id = self._creation_order[i]
            if peer_id in self._pool:
                lock = self._locks.get(peer_id)
                if lock is None or not lock.locked():
                    self._close_sync(peer_id)
                    self._creation_order.pop(i)
                    return
        logger.debug("Pool at capacity with all connections busy, skipping eviction")

    def _close_sync(self, peer_id: str):
        """Close a connection synchronously (best-effort)."""
        if peer_id in self._pool:
            _, writer = self._pool.pop(peer_id)
            try:
                writer.close()
            except Exception:
                pass
            self._last_used.pop(peer_id, None)
            self._conn_hosts.pop(peer_id, None)
            # Do NOT remove lock — peer may reconnect soon

    async def evict_idle(self, idle_timeout: float = 300.0):
        """Close connections unused for idle_timeout seconds."""
        now = time.monotonic()
        to_remove = [
            pid for pid, last in self._last_used.items()
            if now - last > idle_timeout
        ]
        for peer_id in to_remove:
            await self._close_conn(peer_id)
        if to_remove:
            logger.debug(f"Pool: evicted {len(to_remove)} idle connections")

    async def remove(self, peer_id: str):
        """Public interface: remove a dead peer's connection."""
        await self._close_conn(peer_id)

    async def close_all(self):
        """Shutdown: close all connections."""
        for peer_id in list(self._pool.keys()):
            await self._close_conn(peer_id)

    @property
    def size(self) -> int:
        return len(self._pool)
