"""SA-02: Async read offload via asyncio.to_thread for cache misses.

When the StorageCacheProxy has a cache miss, this module offloads the
underlying Storage read to a thread (WAL mode allows parallel readers).

The Storage class itself has sync fallback async_* methods. When this
plugin is loaded, it patches the proxy to use the threaded version.
"""

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AsyncStorageMixin:
    """Mixin that patches a StorageCacheProxy with thread-offloaded async reads.

    The plugin's handler adds this mixin to the proxy instance after SA-01 wraps storage.
    Callers already use await storage.async_get_peers() — no caller changes needed
    once the plugin is loaded.
    """

    async def async_get_peers(self):
        """SA-02: Offload get_peers to thread on cache miss."""
        # Check cache first (synchronous) — cache hits are fast enough to skip thread
        key = "peers:all"
        entry = self._cache.get(key)
        if entry is not None:
            value, expires_at = entry
            if time.monotonic() < expires_at:
                self._hits += 1
                return value
        # Cache miss: offload to thread
        result = await asyncio.to_thread(self._storage.get_peers)
        self._cache[key] = (result, time.monotonic() + self._peers_ttl)
        self._misses += 1
        return result

    async def async_get_peer_by_id(self, node_id: str):
        """SA-02: Offload get_peer_by_id to thread on cache miss."""
        key = f"peers:id:{node_id}"
        entry = self._cache.get(key)
        if entry is not None:
            value, expires_at = entry
            if time.monotonic() < expires_at:
                self._hits += 1
                return value
        result = await asyncio.to_thread(self._storage.get_peer_by_id, node_id)
        self._cache[key] = (result, time.monotonic() + self._peers_ttl)
        self._misses += 1
        return result

    async def async_query_all_active_skills(self, skill_name=None, tag=None, limit=None):
        """SA-02: Offload query_all_active_skills to thread on cache miss."""
        key = f"skills:all:{skill_name}:{tag}:{limit}"
        entry = self._cache.get(key)
        if entry is not None:
            value, expires_at = entry
            if time.monotonic() < expires_at:
                self._hits += 1
                return value
        result = await asyncio.to_thread(
            self._storage.query_all_active_skills, skill_name, tag, limit
        )
        self._cache[key] = (result, time.monotonic() + self._skills_ttl)
        self._misses += 1
        return result

    async def async_get_own_skills(self):
        """SA-02: Offload get_own_skills to thread on cache miss."""
        key = "skills:own"
        entry = self._cache.get(key)
        if entry is not None:
            value, expires_at = entry
            if time.monotonic() < expires_at:
                self._hits += 1
                return value
        result = await asyncio.to_thread(self._storage.get_own_skills)
        self._cache[key] = (result, time.monotonic() + self._own_skills_ttl)
        self._misses += 1
        return result

    async def async_get_ledger_balance(self, peer_public_key: str):
        """SA-02: Offload get_ledger_balance to thread on cache miss."""
        key = f"economy:balance:{peer_public_key}"
        entry = self._cache.get(key)
        if entry is not None:
            value, expires_at = entry
            if time.monotonic() < expires_at:
                self._hits += 1
                return value
        result = await asyncio.to_thread(self._storage.get_ledger_balance, peer_public_key)
        self._cache[key] = (result, time.monotonic() + self._economy_ttl)
        self._misses += 1
        return result


def patch_proxy_with_async_reads(proxy) -> None:
    """Inject async read methods into a StorageCacheProxy instance.

    Called by the plugin handler after wrapping storage. This makes the
    proxy's async_* methods use thread offloading instead of sync passthrough.
    """
    for method_name in [
        "async_get_peers",
        "async_get_peer_by_id",
        "async_query_all_active_skills",
        "async_get_own_skills",
        "async_get_ledger_balance",
    ]:
        method = getattr(AsyncStorageMixin, method_name)
        import types
        setattr(proxy, method_name, types.MethodType(method, proxy))
    logger.debug("ASYNC_READS_PATCHED proxy=%s", type(proxy).__name__)
