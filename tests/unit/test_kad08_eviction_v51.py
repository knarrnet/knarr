"""KAD-08: Min-heap eviction in ProviderCache."""
import sys
import os
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'plugins', '01-kademlia'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from providers import ProviderCache


def test_oldest_record_evicted_correctly():
    """When cache is full, the oldest stored record must be evicted."""
    cache = ProviderCache(max_records=3)

    # Store 3 records with distinct store times
    # We manipulate stored_at directly by calling store and then adjusting
    cache.store("skill-a", "a" * 64, "10.0.0.1", 9001, 0, ttl=3600)
    cache.store("skill-b", "b" * 64, "10.0.0.2", 9002, 0, ttl=3600)
    cache.store("skill-c", "c" * 64, "10.0.0.3", 9003, 0, ttl=3600)

    # Force skill-a to be the oldest by backdating its stored_at
    key_a = cache._get_key("skill-a")
    cache.cache[key_a]["a" * 64]["stored_at"] = 0.001  # very old

    # Clear and rebuild heap so the backdated record is at the top
    import heapq
    cache._evict_heap = []
    for key, providers in cache.cache.items():
        for node_id, rec in providers.items():
            heapq.heappush(cache._evict_heap, (rec["stored_at"], key, node_id))

    # Adding a 4th record should evict skill-a
    cache.store("skill-d", "d" * 64, "10.0.0.4", 9004, 0, ttl=3600)

    assert cache.get_providers("skill-a") == [], "Oldest record (skill-a) must be evicted"
    assert len(cache.get_providers("skill-d")) == 1, "Newest record must be present"
    assert cache._total_records == 3


def test_heap_consistent_after_multiple_evictions():
    """Multiple evictions must leave heap consistent (no corrupted state)."""
    cache = ProviderCache(max_records=2)

    for i in range(5):
        cache.store(f"skill-{i}", "a" * 64, "10.0.0.1", 9000 + i, 0, ttl=3600)

    # After 5 insertions with max=2, we should have exactly 2 records
    assert cache._total_records == 2


def test_lazy_stale_entry_cleanup_does_not_break_eviction():
    """Heap entries for removed records must be skipped without breaking eviction."""
    import heapq

    cache = ProviderCache(max_records=3)

    cache.store("skill-x", "x" * 64, "10.0.0.1", 9001, 0, ttl=3600)
    cache.store("skill-y", "y" * 64, "10.0.0.2", 9002, 0, ttl=3600)

    # Manually push a stale entry (simulate removed record)
    cache._evict_heap = []
    heapq.heappush(cache._evict_heap, (0.0, "stale-key-hash", "stale-node-id"))
    for key, providers in cache.cache.items():
        for node_id, rec in providers.items():
            heapq.heappush(cache._evict_heap, (rec["stored_at"], key, node_id))

    # Backdate skill-x to be evicted
    key_x = cache._get_key("skill-x")
    cache.cache[key_x]["x" * 64]["stored_at"] = 0.5
    cache._evict_heap = []
    heapq.heappush(cache._evict_heap, (0.0, "stale-key-hash-2", "z" * 64))  # stale
    heapq.heappush(cache._evict_heap, (0.5, key_x, "x" * 64))
    for key, providers in cache.cache.items():
        for node_id, rec in providers.items():
            if not (key == key_x and node_id == "x" * 64):
                heapq.heappush(cache._evict_heap, (rec["stored_at"], key, node_id))

    # Now fill to capacity — should evict skill-x, skip stale entry
    cache.store("skill-z", "z" * 64, "10.0.0.3", 9003, 0, ttl=3600)

    # skill-x (with stored_at=0.5) evicted; stale entry was skipped
    assert cache.get_providers("skill-x") == [], "skill-x must be evicted"
    assert len(cache.get_providers("skill-z")) == 1


def test_eviction_respects_max_records():
    """Cache must never exceed max_records after multiple insertions."""
    max_r = 10
    cache = ProviderCache(max_records=max_r)

    for i in range(50):
        # Each store to same skill key for node i
        cache.store("shared-skill", format(i, '064x'), "10.0.0.1", 9000, 0, ttl=3600)

    assert cache._total_records <= max_r, f"Must not exceed max_records={max_r}"
