"""SA-02: Async read offload via asyncio.to_thread.

Tests verify:
- async_get_peers/etc exist as fallback methods on Storage (sync pass-through)
- Cache hit path is synchronous (no thread)
- Thread-offloaded path is used on cache miss after plugin patches proxy
- Fallback works without plugin (Storage.async_get_peers returns same as get_peers)
"""

import asyncio
import sys
import os
import pytest
from unittest.mock import MagicMock, patch


# ──────────────────────────────────────────────────────────────────────────────
# SA-02-A: Storage has async_* fallback methods
# ──────────────────────────────────────────────────────────────────────────────

def test_storage_has_async_fallbacks():
    """Storage class must have async_get_peers, async_get_peer_by_id, etc."""
    from knarr.dht.storage import Storage
    for method_name in [
        "async_get_peers",
        "async_get_peer_by_id",
        "async_query_all_active_skills",
        "async_get_own_skills",
        "async_get_ledger_balance",
    ]:
        assert hasattr(Storage, method_name), f"Storage missing {method_name}"
        assert asyncio.iscoroutinefunction(getattr(Storage, method_name)), (
            f"Storage.{method_name} must be a coroutine"
        )


# ──────────────────────────────────────────────────────────────────────────────
# SA-02-B: Storage.async_get_peers fallback returns same as get_peers
# ──────────────────────────────────────────────────────────────────────────────

def test_storage_async_get_peers_fallback():
    """async_get_peers on a plain Storage must return same as get_peers."""
    from knarr.dht.storage import Storage
    storage = Storage(":memory:")

    sync_result = storage.get_peers()
    async_result = asyncio.get_event_loop().run_until_complete(storage.async_get_peers())

    assert sync_result == async_result


# ──────────────────────────────────────────────────────────────────────────────
# SA-02-C: StorageCacheProxy.async_get_peers after patching uses to_thread
# ──────────────────────────────────────────────────────────────────────────────

def test_proxy_async_reads_uses_to_thread():
    """After patch_proxy_with_async_reads, cache miss must use asyncio.to_thread."""
    _plugin_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "plugins", "00-storage-strategy"
    )
    if _plugin_dir not in sys.path:
        sys.path.insert(0, os.path.abspath(_plugin_dir))

    from cache import StorageCacheProxy
    from async_reads import patch_proxy_with_async_reads

    mock_storage = MagicMock()
    mock_storage.get_peers.return_value = [{"node_id": "a" * 64}]
    proxy = StorageCacheProxy(mock_storage, {"peers_ttl": 30})
    patch_proxy_with_async_reads(proxy)

    thread_calls = []
    original_to_thread = asyncio.to_thread

    async def mock_to_thread(fn, *args, **kwargs):
        thread_calls.append(fn.__name__ if hasattr(fn, "__name__") else str(fn))
        return fn(*args, **kwargs)

    with patch("asyncio.to_thread", side_effect=mock_to_thread):
        result = asyncio.get_event_loop().run_until_complete(proxy.async_get_peers())

    # On cache miss, to_thread must be called
    assert len(thread_calls) > 0


# ──────────────────────────────────────────────────────────────────────────────
# SA-02-D: Cache hit in async path is synchronous (no thread)
# ──────────────────────────────────────────────────────────────────────────────

def test_async_cache_hit_is_synchronous():
    """When cache is warm, async_get_peers must return immediately (no to_thread)."""
    _plugin_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "plugins", "00-storage-strategy"
    )
    if _plugin_dir not in sys.path:
        sys.path.insert(0, os.path.abspath(_plugin_dir))

    from cache import StorageCacheProxy
    from async_reads import patch_proxy_with_async_reads

    mock_storage = MagicMock()
    mock_storage.get_peers.return_value = [{"node_id": "a" * 64}]
    proxy = StorageCacheProxy(mock_storage, {"peers_ttl": 30})
    patch_proxy_with_async_reads(proxy)

    # Warm the cache with a sync read
    _ = proxy.get_peers()

    thread_calls = []

    async def mock_to_thread(fn, *args, **kwargs):
        thread_calls.append(fn)
        return fn(*args, **kwargs)

    with patch("asyncio.to_thread", side_effect=mock_to_thread):
        result = asyncio.get_event_loop().run_until_complete(proxy.async_get_peers())

    # Cache was warm — to_thread should NOT have been called
    assert len(thread_calls) == 0, "async_get_peers should not use to_thread on cache hit"


# ──────────────────────────────────────────────────────────────────────────────
# SA-02-E: Storage.async_get_peer_by_id fallback
# ──────────────────────────────────────────────────────────────────────────────

def test_storage_async_get_peer_by_id_fallback():
    """async_get_peer_by_id on plain Storage must match sync result."""
    from knarr.dht.storage import Storage
    storage = Storage(":memory:")

    sync_result = storage.get_peer_by_id("a" * 64)
    async_result = asyncio.get_event_loop().run_until_complete(
        storage.async_get_peer_by_id("a" * 64)
    )
    assert sync_result == async_result
