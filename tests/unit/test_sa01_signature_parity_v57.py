"""SA-01/SA-02 signature-parity contract test — v0.57.0 hotfix.

Root cause of gate-run2 blocker: the v0.52.0 storage-strategy plugin was
written against an imaginary DHTStorage API. cache.py and async_reads.py had
`query_all_active_skills(skill_name, tag, limit)` wrappers, but the sync
method had signature `(peer_timeout, limit)` since v0.33.0. Broken since
v0.52.0; undetected because existing SA-01/SA-02 tests used MagicMock, which
accepts any signature.

This file is a signature-parity contract: instantiate REAL DHTStorage(":memory:"),
wrap with StorageCacheProxy, patch with AsyncStorageMixin, and call every
wrapped read method with both the zero-arg form (core caller pattern) and
the natural kwargs form. Any TypeError or AttributeError is a bug.
"""

import asyncio
import os
import sys

import pytest

_plugin_dir = os.path.join(
    os.path.dirname(__file__), "..", "..", "plugins", "00-storage-strategy"
)
if _plugin_dir not in sys.path:
    sys.path.insert(0, os.path.abspath(_plugin_dir))

from knarr.dht.storage import Storage
from cache import StorageCacheProxy
from async_reads import patch_proxy_with_async_reads


@pytest.fixture
def proxy():
    storage = Storage(":memory:")
    return StorageCacheProxy(storage, {"peers_ttl": 30, "skills_ttl": 60})


@pytest.fixture
def async_proxy():
    storage = Storage(":memory:")
    p = StorageCacheProxy(storage, {"peers_ttl": 30, "skills_ttl": 60})
    patch_proxy_with_async_reads(p)
    return p


# ──────────────────────────────────────────────────────────────────────────────
# Sync cache-proxy wrappers: signature parity with DHTStorage sync methods
# ──────────────────────────────────────────────────────────────────────────────

def test_query_all_active_skills_zero_args(proxy):
    """Core callers use zero-arg form — must not raise TypeError."""
    result = proxy.query_all_active_skills()
    assert result == []


def test_query_all_active_skills_with_kwargs(proxy):
    """Natural kwargs form — matches sync signature."""
    result = proxy.query_all_active_skills(peer_timeout=120, limit=500)
    assert result == []


def test_get_peers_zero_args(proxy):
    assert proxy.get_peers() == []


def test_get_peer_by_id(proxy):
    assert proxy.get_peer_by_id("a" * 64) is None


def test_get_own_skills_zero_args(proxy):
    assert proxy.get_own_skills() == []


def test_get_ledger_balance(proxy):
    assert proxy.get_ledger_balance("b" * 64) is None


def test_get_economy_stats_delegates_to_raw_storage(proxy):
    """DHTStorage has no get_economy_stats — must delegate via __getattr__
    and raise AttributeError (which PluginContext.get_economy_stats catches
    via getattr(..., None)). Phantom cache override removed in v0.57.0 hotfix."""
    with pytest.raises(AttributeError):
        proxy.get_economy_stats()


# ──────────────────────────────────────────────────────────────────────────────
# Async wrappers: signature parity via AsyncStorageMixin (plugin patch path)
# ──────────────────────────────────────────────────────────────────────────────

def test_async_query_all_active_skills_zero_args_via_plugin(async_proxy):
    result = asyncio.new_event_loop().run_until_complete(
        async_proxy.async_query_all_active_skills()
    )
    assert result == []


def test_async_query_all_active_skills_with_kwargs_via_plugin(async_proxy):
    result = asyncio.new_event_loop().run_until_complete(
        async_proxy.async_query_all_active_skills(peer_timeout=120, limit=500)
    )
    assert result == []


def test_async_get_peers_via_plugin(async_proxy):
    assert asyncio.new_event_loop().run_until_complete(async_proxy.async_get_peers()) == []


def test_async_get_peer_by_id_via_plugin(async_proxy):
    result = asyncio.new_event_loop().run_until_complete(
        async_proxy.async_get_peer_by_id("a" * 64)
    )
    assert result is None


def test_async_get_own_skills_via_plugin(async_proxy):
    assert asyncio.new_event_loop().run_until_complete(async_proxy.async_get_own_skills()) == []


def test_async_get_ledger_balance_via_plugin(async_proxy):
    result = asyncio.new_event_loop().run_until_complete(
        async_proxy.async_get_ledger_balance("b" * 64)
    )
    assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# Core Storage async fallbacks: signature parity (non-plugin path)
# ──────────────────────────────────────────────────────────────────────────────

def test_storage_async_query_all_active_skills_zero_args():
    storage = Storage(":memory:")
    result = asyncio.new_event_loop().run_until_complete(
        storage.async_query_all_active_skills()
    )
    assert result == []


def test_storage_async_query_all_active_skills_with_kwargs():
    storage = Storage(":memory:")
    result = asyncio.new_event_loop().run_until_complete(
        storage.async_query_all_active_skills(peer_timeout=120, limit=500)
    )
    assert result == []
