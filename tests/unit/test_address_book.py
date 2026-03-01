import pytest
import time
from knarr.dht.storage import Storage

@pytest.fixture
def storage():
    return Storage(":memory:")

def test_address_book_tier_priority(storage):
    node_id = "peer1"
    
    # Add remote tier
    storage.upsert_address(node_id, tier="remote", last_ip="1.1.1.1")
    # Add cached tier
    storage.upsert_address(node_id, tier="cached", last_ip="2.2.2.2")
    
    # Should return cached tier (priority 2) over remote (priority 3)
    addr = storage.get_address(node_id)
    assert addr["tier"] == "cached"
    assert addr["last_ip"] == "2.2.2.2"
    
    # Add explicit tier
    storage.upsert_address(node_id, tier="explicit", last_ip="3.3.3.3")
    
    # Should return explicit tier (priority 1)
    addr = storage.get_address(node_id)
    assert addr["tier"] == "explicit"
    assert addr["last_ip"] == "3.3.3.3"

def test_address_book_caching(storage):
    storage.upsert_address("p1", tier="cached")
    storage.upsert_address("p2", tier="cached")
    
    cached = storage.get_addresses_by_tier("cached")
    assert len(cached) == 2
    
    storage.evict_cached_addresses(max_entries=1)
    cached = storage.get_addresses_by_tier("cached")
    assert len(cached) == 1
