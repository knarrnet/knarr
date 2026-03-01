import asyncio
import logging
import time
from typing import Optional, Dict, Tuple, List
from ..core.messages import Message
from .protocol import send_message, receive_message

logger = logging.getLogger(__name__)

_SEND_FAILED = object()  # sentinel for failed send attempts


class ConnectionPool:
    """Persistent TCP connections to known peers. LIFO eviction.

    All operations on a given peer are serialized by a per-peer asyncio.Lock.
    The lock covers check/create/send/receive so there is no TOCTOU window
    for duplicate connections or interleaved stream writes.
    """

    def __init__(self, max_connections: int = 50):
        self._pool: Dict[str, Tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._lock_guard = asyncio.Lock()  # protects _locks dict creation
        self._max = max_connections
        self._last_used: Dict[str, float] = {}
        self._creation_order: List[str] = []  # newest last — LIFO evicts from end

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

    async def _open(self, peer_id: str, host: str, port: int,
                    timeout: float) -> Optional[Tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
        """Create a TCP connection and store it in the pool. Returns the pair or None."""
        if len(self._pool) >= self._max:
            self._evict_lifo()

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
        except Exception:
            return None

        self._pool[peer_id] = (reader, writer)
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
            if peer_id in self._creation_order:
                self._creation_order.remove(peer_id)

    async def _try_send(self, reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter, msg: Message,
                        timeout: float) -> object:
        """Send + receive on an existing connection. Returns response or _SEND_FAILED."""
        try:
            await send_message(writer, msg)
            response = await asyncio.wait_for(receive_message(reader), timeout=timeout)
            if response is None:
                return _SEND_FAILED
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
        """
        lock = await self._get_lock(peer_id)
        async with lock:
            # 1. Try existing connection
            conn = self._pool.get(peer_id)
            if conn is not None and self._is_healthy(conn):
                result = await self._try_send(conn[0], conn[1], msg, timeout)
                if result is not _SEND_FAILED:
                    self._last_used[peer_id] = time.monotonic()
                    return result
                # Broken — close and fall through to fresh connection
                await self._close_conn(peer_id)
            elif conn is not None:
                # Unhealthy — close stale connection
                await self._close_conn(peer_id)

            # 2. Fresh connection + send (first attempt)
            conn = await self._open(peer_id, host, port, timeout)
            if conn is None:
                return None

            result = await self._try_send(conn[0], conn[1], msg, timeout)
            if result is not _SEND_FAILED:
                self._last_used[peer_id] = time.monotonic()
                return result

            # 3. Retry once with brand new connection
            await self._close_conn(peer_id)
            conn = await self._open(peer_id, host, port, timeout)
            if conn is None:
                return None

            result = await self._try_send(conn[0], conn[1], msg, timeout)
            if result is not _SEND_FAILED:
                self._last_used[peer_id] = time.monotonic()
                return result

            await self._close_conn(peer_id)
            return None

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
