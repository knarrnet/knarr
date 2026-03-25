"""SA-01: StorageCacheProxy — TTL-based in-memory cache for Storage reads.

Tests verify:
- Cache hit returns same object (no DB re-read within TTL)
- Cache miss hits DB
- Write methods invalidate cache
- TTL expiry causes cache refresh
- Uncached methods pass through via __getattr__
- get_async_job is NOT cached
"""

import sys
import os
import time
import pytest
from unittest.mock import MagicMock, call

# Make plugin directory importable
_plugin_dir = os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "00-storage-strategy")
if _plugin_dir not in sys.path:
    sys.path.insert(0, os.path.abspath(_plugin_dir))

from cache import StorageCacheProxy


def make_proxy(ttl_config=None):
    """Create a StorageCacheProxy wrapping a mock storage."""
    mock_storage = MagicMock()
    mock_storage.get_peers.return_value = [{"node_id": "a" * 64}]
    mock_storage.get_peer_by_id.return_value = {"node_id": "b" * 64}
    mock_storage.query_all_active_skills.return_value = [{"name": "test-skill"}]
    mock_storage.get_own_skills.return_value = [{"name": "own-skill"}]
    mock_storage.get_ledger_balance.return_value = 5.0
    mock_storage.get_async_job.return_value = {"status": "running"}
    proxy = StorageCacheProxy(mock_storage, ttl_config or {"peers_ttl": 30})
    return proxy, mock_storage


# ──────────────────────────────────────────────────────────────────────────────
# SA-01-A: Cache hit returns same object within TTL
# ──────────────────────────────────────────────────────────────────────────────

def test_cache_hit_returns_same_result():
    """Second call within TTL must return cached result without hitting DB."""
    proxy, mock_storage = make_proxy()

    result1 = proxy.get_peers()
    result2 = proxy.get_peers()

    assert result1 is result2
    mock_storage.get_peers.assert_called_once()  # DB hit only on first call


# ──────────────────────────────────────────────────────────────────────────────
# SA-01-B: Cache miss hits DB
# ──────────────────────────────────────────────────────────────────────────────

def test_cache_miss_hits_db():
    """First call must fetch from DB (cache miss)."""
    proxy, mock_storage = make_proxy()
    stats_before = proxy.cache_stats()
    _ = proxy.get_peers()
    stats_after = proxy.cache_stats()
    mock_storage.get_peers.assert_called_once()
    assert stats_after["misses"] == stats_before["misses"] + 1


# ──────────────────────────────────────────────────────────────────────────────
# SA-01-C: Write invalidates cache
# ──────────────────────────────────────────────────────────────────────────────

def test_write_invalidates_cache():
    """upsert_peer must invalidate all peers:* cache entries."""
    proxy, mock_storage = make_proxy()

    _ = proxy.get_peers()
    assert mock_storage.get_peers.call_count == 1

    # Write invalidates
    proxy.upsert_peer("b" * 64, "1.2.3.4", 9001, time.time())
    mock_storage.upsert_peer.assert_called_once()

    # Next read should hit DB again
    _ = proxy.get_peers()
    assert mock_storage.get_peers.call_count == 2


# ──────────────────────────────────────────────────────────────────────────────
# SA-01-D: TTL expiry causes cache refresh
# ──────────────────────────────────────────────────────────────────────────────

def test_ttl_expiry_causes_refresh():
    """After TTL expires, cache must be refreshed from DB."""
    proxy, mock_storage = make_proxy(ttl_config={"peers_ttl": 0.01})  # 10ms TTL

    _ = proxy.get_peers()
    assert mock_storage.get_peers.call_count == 1

    # Wait for TTL to expire
    time.sleep(0.02)

    _ = proxy.get_peers()
    assert mock_storage.get_peers.call_count == 2  # DB hit again


# ──────────────────────────────────────────────────────────────────────────────
# SA-01-E: Uncached methods pass through via __getattr__
# ──────────────────────────────────────────────────────────────────────────────

def test_uncached_methods_pass_through():
    """Methods not explicitly cached must pass through to the underlying storage."""
    proxy, mock_storage = make_proxy()

    # insert_task is not cached — should pass through
    proxy.insert_task("t1")
    mock_storage.insert_task.assert_called_once_with("t1")

    # get_async_job is not cached — should pass through
    result = proxy.get_async_job("job1")
    mock_storage.get_async_job.assert_called_once_with("job1")


# ──────────────────────────────────────────────────────────────────────────────
# SA-01-F: get_async_job is NOT cached
# ──────────────────────────────────────────────────────────────────────────────

def test_get_async_job_not_cached():
    """get_async_job must always go to DB (polled for status changes)."""
    proxy, mock_storage = make_proxy()

    _ = proxy.get_async_job("job1")
    _ = proxy.get_async_job("job1")
    assert mock_storage.get_async_job.call_count == 2  # Both calls hit DB


# ──────────────────────────────────────────────────────────────────────────────
# SA-01-G: skills write invalidates skills cache
# ──────────────────────────────────────────────────────────────────────────────

def test_skill_write_invalidates_skill_cache():
    """upsert_skill must invalidate all skills:* cache entries."""
    proxy, mock_storage = make_proxy()

    _ = proxy.get_own_skills()
    assert mock_storage.get_own_skills.call_count == 1

    proxy.upsert_skill("key", "node", {}, 3600, True, None, None)
    _ = proxy.get_own_skills()
    assert mock_storage.get_own_skills.call_count == 2


# ──────────────────────────────────────────────────────────────────────────────
# SA-01-H: cache_stats returns hit/miss/size
# ──────────────────────────────────────────────────────────────────────────────

def test_cache_stats():
    """cache_stats must return hits, misses, and current cache size."""
    proxy, mock_storage = make_proxy()
    _ = proxy.get_peers()
    _ = proxy.get_peers()  # cache hit
    stats = proxy.cache_stats()
    assert stats["hits"] >= 1
    assert stats["misses"] >= 1
    assert "size" in stats
