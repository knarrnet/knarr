"""SA-01: StorageCacheProxy — TTL-based in-memory cache for Storage reads.

Design principles:
- __getattr__ delegates EVERYTHING not explicitly overridden — no breakage
- _cached_read(key, fn, ttl) uses tuple (value, expires_at) — no object overhead
- Write methods call raw storage THEN invalidate — never stale after write
- Prefix-based invalidation: upsert_peer invalidates all peers:* cache keys
- get_async_job is NOT cached (polled for status changes)
"""

import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Default TTLs (seconds)
DEFAULT_PEERS_TTL = 30
DEFAULT_SKILLS_TTL = 60
DEFAULT_ECONOMY_TTL = 10
DEFAULT_OWN_SKILLS_TTL = 120


class StorageCacheProxy:
    """Proxy that wraps a Storage instance with TTL-based in-memory caching.

    Intercepts read methods for hot paths. All other methods are forwarded
    transparently via __getattr__.
    """

    def __init__(self, storage, ttl_config: Optional[Dict[str, Any]] = None):
        # SA-01: Use object.__setattr__ during __init__ to prevent __getattr__
        # recursion before _storage is set (D's robustness pattern).
        object.__setattr__(self, '_storage', storage)
        ttl_config = ttl_config or {}
        object.__setattr__(self, '_peers_ttl', float(ttl_config.get("peers_ttl", DEFAULT_PEERS_TTL)))
        object.__setattr__(self, '_skills_ttl', float(ttl_config.get("skills_ttl", DEFAULT_SKILLS_TTL)))
        object.__setattr__(self, '_economy_ttl', float(ttl_config.get("economy_ttl", DEFAULT_ECONOMY_TTL)))
        object.__setattr__(self, '_own_skills_ttl', float(ttl_config.get("own_skills_ttl", DEFAULT_OWN_SKILLS_TTL)))
        # Cache: key -> (value, expires_at)
        object.__setattr__(self, '_cache', {})
        object.__setattr__(self, '_hits', 0)
        object.__setattr__(self, '_misses', 0)
        object.__setattr__(self, '_debug', bool(ttl_config.get("debug", False)))

    def __getattr__(self, name: str):
        """Delegate all attribute lookups to the underlying storage."""
        return getattr(self._storage, name)

    # ──────────────────────────────────────────────────────────────────────────
    # Internal cache helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _cached_read(self, key: str, fn: Callable, ttl: float) -> Any:
        """Return cached value if fresh; otherwise call fn(), cache, and return."""
        now = time.monotonic()
        entry = self._cache.get(key)
        if entry is not None:
            value, expires_at = entry
            if now < expires_at:
                self._hits += 1
                return value
        t0 = time.monotonic()
        result = fn()
        elapsed = time.monotonic() - t0
        self._cache[key] = (result, now + ttl)
        self._misses += 1
        if self._debug:
            logger.info("CACHE_MISS key=%s db_ms=%.1f total=%d/%d",
                        key, elapsed * 1000, self._misses, self._hits + self._misses)
        return result

    def _invalidate_prefix(self, prefix: str) -> None:
        """Remove all cache entries whose key starts with prefix."""
        keys_to_drop = [k for k in self._cache if k.startswith(prefix)]
        for k in keys_to_drop:
            del self._cache[k]
        if keys_to_drop:
            logger.debug("CACHE_INVALIDATE prefix=%s dropped=%d", prefix, len(keys_to_drop))

    def _invalidate_key(self, key: str) -> None:
        """Remove a specific cache entry."""
        self._cache.pop(key, None)

    def cache_stats(self) -> Dict[str, int]:
        """Return cache hit/miss counters and current size."""
        return {"hits": self._hits, "misses": self._misses, "size": len(self._cache)}

    # ──────────────────────────────────────────────────────────────────────────
    # Cached read methods
    # ──────────────────────────────────────────────────────────────────────────

    def get_peers(self):
        return self._cached_read("peers:all", self._storage.get_peers, self._peers_ttl)

    def get_peer_by_id(self, node_id: str):
        key = f"peers:id:{node_id}"
        return self._cached_read(key, lambda: self._storage.get_peer_by_id(node_id), self._peers_ttl)

    def query_all_active_skills(self, skill_name=None, tag=None, limit=None):
        cache_key = f"skills:all:{skill_name}:{tag}:{limit}"
        return self._cached_read(
            cache_key,
            lambda: self._storage.query_all_active_skills(skill_name, tag, limit),
            self._skills_ttl,
        )

    def get_own_skills(self):
        return self._cached_read(
            "skills:own", self._storage.get_own_skills, self._own_skills_ttl
        )

    def get_economy_stats(self):
        return self._cached_read(
            "economy:stats", self._storage.get_economy_stats, self._economy_ttl
        )

    def get_ledger_balance(self, peer_key: str):
        key = f"economy:balance:{peer_key}"
        return self._cached_read(
            key, lambda: self._storage.get_ledger_balance(peer_key), self._economy_ttl
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Write-through with invalidation
    # ──────────────────────────────────────────────────────────────────────────

    def upsert_peer(self, *args, **kwargs):
        result = self._storage.upsert_peer(*args, **kwargs)
        # Granular invalidation: only invalidate the specific peer entry.
        # peers:all expires via TTL (30s) — one peer updating doesn't meaningfully
        # change the full list. Prefix invalidation here caused cache thrashing:
        # 15 upserts/sec at 150 nodes wiped the cache before any read could hit.
        peer = args[0] if args else None
        if peer and hasattr(peer, 'node_id'):
            self._invalidate_key(f"peers:id:{peer.node_id}")
        return result

    def remove_peer(self, *args, **kwargs):
        result = self._storage.remove_peer(*args, **kwargs)
        # Full prefix invalidation on remove — the peer is gone from the list
        self._invalidate_prefix("peers:")
        return result

    def upsert_skill(self, *args, **kwargs):
        result = self._storage.upsert_skill(*args, **kwargs)
        # Granular: only invalidate specific skill entries, not the full catalog.
        # skills:all expires via TTL (60s). At 150 nodes × 8 skills, prefix
        # invalidation fires 1200 times during join — cache never gets a hit.
        skill_key = args[0] if args else None
        if skill_key:
            self._invalidate_key(f"skills:all:{skill_key}:None:None")
        self._invalidate_key("skills:own")
        return result

    def remove_skill(self, *args, **kwargs):
        result = self._storage.remove_skill(*args, **kwargs)
        # Full prefix invalidation on remove — the skill is gone
        self._invalidate_prefix("skills:")
        return result

    def update_ledger_provider(self, *args, **kwargs):
        result = self._storage.update_ledger_provider(*args, **kwargs)
        self._invalidate_prefix("economy:")
        return result

    def update_ledger_consumer(self, *args, **kwargs):
        result = self._storage.update_ledger_consumer(*args, **kwargs)
        self._invalidate_prefix("economy:")
        return result

    def write_receipt(self, *args, **kwargs):
        result = self._storage.write_receipt(*args, **kwargs)
        self._invalidate_prefix("economy:")
        return result

    # get_async_job is intentionally NOT cached — callers poll it for status changes
