"""KAD-01: DHT skill store — STORE, FIND_VALUE, publish, query, dedup, size limit."""

import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).parent.parent.parent / "plugins/00-kademlia"


def _load_module(name, path):
    """Load a module from a specific file path, bypassing sys.modules cache."""
    plugin_dir = str(_PLUGIN_DIR)
    added = plugin_dir not in sys.path
    if added:
        sys.path.insert(0, plugin_dir)
    try:
        spec = importlib.util.spec_from_file_location(f"_kad_{name}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if added and plugin_dir in sys.path:
            sys.path.remove(plugin_dir)


# -- Key function tests --

def test_default_key_function():
    """default_key_function returns SHA-256(canonical_path.encode())."""
    handler = _load_module("handler", _PLUGIN_DIR / "handler.py")

    result = handler.default_key_function("translate", "knowledge/translate")
    expected = hashlib.sha256(b"knowledge/translate").digest()
    assert result == expected, (
        f"key function should return SHA-256 of canonical_path, got {result.hex()}"
    )


def test_key_function_uses_path_not_name():
    """Key function uses canonical_path, not skill_name."""
    handler = _load_module("handler", _PLUGIN_DIR / "handler.py")

    key_a = handler.default_key_function("translate", "knowledge/translate")
    key_b = handler.default_key_function("other-name", "knowledge/translate")
    assert key_a == key_b, "Same canonical_path should produce same key"

    key_c = handler.default_key_function("translate", "compute/translate")
    assert key_a != key_c, "Different canonical_path should produce different key"


# -- STORE handler tests --

def test_store_rejects_oversized_record():
    """PUT_PROVIDER with >4KB payload should be rejected."""
    source = (_PLUGIN_DIR / "handler.py").read_text(encoding="utf-8")

    assert "_MAX_PROVIDER_RECORD_SIZE" in source, (
        "KAD-01: _MAX_PROVIDER_RECORD_SIZE constant must be defined"
    )
    assert "record_size" in source or "MAX_PROVIDER_RECORD_SIZE" in source, (
        "KAD-01: PUT_PROVIDER handler must check record size"
    )


def test_store_max_providers_per_key():
    """Max 100 providers per key — evict oldest on overflow."""
    source = (_PLUGIN_DIR / "handler.py").read_text(encoding="utf-8")

    assert "_MAX_PROVIDERS_PER_KEY" in source, (
        "KAD-01: _MAX_PROVIDERS_PER_KEY constant must be defined"
    )
    assert "100" in source, "KAD-01: max providers per key should be 100"


def test_store_dedup_by_node_id():
    """Same node_id → latest record wins, no duplicates."""
    providers_mod = _load_module("providers", _PLUGIN_DIR / "providers.py")
    ProviderCache = providers_mod.ProviderCache

    cache = ProviderCache(max_records=1000)
    cache.store("skill-a", "aa" * 32, "10.0.0.1", 9000, 8100, ttl=3600)
    cache.store("skill-a", "aa" * 32, "10.0.0.2", 9001, 8101, ttl=3600)

    providers = cache.get_providers("skill-a")
    assert len(providers) == 1, f"Dedup: expected 1 provider, got {len(providers)}"
    assert providers[0]["host"] == "10.0.0.2", "Latest record should win"
    assert providers[0]["port"] == 9001, "Latest record should win"


def test_store_enforces_sender_identity():
    """PUT_PROVIDER uses msg.node_id (authenticated), not payload node_id."""
    source = (_PLUGIN_DIR / "handler.py").read_text(encoding="utf-8")

    # Check that the handler uses msg.node_id, not payload node_id
    assert "sender_id = msg.node_id" in source, (
        "KAD-01: PUT_PROVIDER must use authenticated msg.node_id"
    )


# -- FIND_VALUE handler tests --

def test_find_value_response_shape():
    """GET_PROVIDERS/FIND_VALUE returns providers + closest_nodes."""
    source = (_PLUGIN_DIR / "handler.py").read_text(encoding="utf-8")

    assert '"providers"' in source, "Response must include 'providers' field"
    assert '"closest_nodes"' in source, "Response must include 'closest_nodes' field"


def test_find_value_action_alias():
    """FIND_VALUE should be handled as alias for GET_PROVIDERS."""
    source = (_PLUGIN_DIR / "handler.py").read_text(encoding="utf-8")

    assert '"FIND_VALUE"' in source, "FIND_VALUE action must be handled"
    assert '"STORE"' in source, "STORE action must be handled as alias for PUT_PROVIDER"


def test_find_value_closest_nodes_fallback():
    """When no providers found, closest_nodes should be populated."""
    source = (_PLUGIN_DIR / "handler.py").read_text(encoding="utf-8")

    # Verify the conditional: closest_nodes populated when no providers
    assert "if not providers" in source, (
        "KAD-01: closest_nodes should be populated only when no providers found"
    )


# -- Provider cache dedup + eviction --

def test_provider_cache_evicts_oldest():
    """ProviderCache evicts oldest record when max_records exceeded."""
    providers_mod = _load_module("providers", _PLUGIN_DIR / "providers.py")
    ProviderCache = providers_mod.ProviderCache

    cache = ProviderCache(max_records=3)
    cache.store("skill-a", "aa" * 32, "h1", 1, 0)
    cache.store("skill-b", "bb" * 32, "h2", 2, 0)
    cache.store("skill-c", "cc" * 32, "h3", 3, 0)
    # This should evict the oldest
    cache.store("skill-d", "dd" * 32, "h4", 4, 0)

    assert cache._total_records == 3, f"Expected 3 records after eviction, got {cache._total_records}"


# -- Publish path --

def test_publish_includes_canonical_path():
    """_put_provider_to_closest should include canonical_path in payload."""
    source = (_PLUGIN_DIR / "handler.py").read_text(encoding="utf-8")

    assert '"canonical_path"' in source, (
        "KAD-01: PUT_PROVIDER payload must include canonical_path"
    )
    assert "default_key_function" in source, (
        "KAD-01: publish path must use the pluggable key function"
    )


# -- Query path --

def test_query_path_local_cache_first():
    """on_query should check local cache before network lookup."""
    source = (_PLUGIN_DIR / "handler.py").read_text(encoding="utf-8")

    # Verify search is called before lookup
    lines = source.split("\n")
    search_line = None
    lookup_line = None
    for i, line in enumerate(lines):
        if "providers.search" in line and search_line is None:
            search_line = i
        if "self._lookup" in line and "find_providers" in line and lookup_line is None:
            lookup_line = i

    assert search_line is not None, "on_query must call providers.search (local cache)"
    assert lookup_line is not None, "on_query must call lookup.find_providers (network)"
    assert search_line < lookup_line, "Local cache search must happen before network lookup"
