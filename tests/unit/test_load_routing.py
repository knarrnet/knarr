import time
import pytest
from knarr.dht.node import DHTNode

def test_ranking_prefers_lower_load():
    node = DHTNode("127.0.0.1", 0)
    
    now = time.time()
    results = [
        {
            "node_id": "p_high_load",
            "host": "h", "port": 0,
            "skill_sheet": {"name": "s"},
            "_last_seen": now - 10,
            "_announced_at": now - 10,
            "_load": 8
        },
        {
            "node_id": "p_low_load",
            "host": "h", "port": 0,
            "skill_sheet": {"name": "s"},
            "_last_seen": now - 10,
            "_announced_at": now - 10,
            "_load": 2
        }
    ]
    
    ranked = node._rank_results(results)
    # Lower load (2) should be first
    assert ranked[0]["node_id"] == "p_low_load"

def test_ranking_unknown_load_neutral():
    node = DHTNode("127.0.0.1", 0)
    
    now = time.time()
    results = [
        {
            "node_id": "p_high_load",
            "host": "h", "port": 0,
            "skill_sheet": {"name": "s"},
            "_last_seen": now - 10,
            "_announced_at": now - 10,
            "_load": 9
        },
        {
            "node_id": "p_unknown_load",
            "host": "h", "port": 0,
            "skill_sheet": {"name": "s"},
            "_last_seen": now - 10,
            "_announced_at": now - 10,
            "_load": -1
        }
    ]
    
    ranked = node._rank_results(results)
    # Unknown load (0.5 availability) should be better than high load (0.1 availability)
    assert ranked[0]["node_id"] == "p_unknown_load"

def test_ranking_weights_balanced():
    node = DHTNode("127.0.0.1", 0)
    
    now = time.time()
    # Provider A: low load (good), old freshness (bad)
    # Provider B: high load (bad), new freshness (good)
    results = [
        {
            "node_id": "p_a",
            "host": "h", "port": 0,
            "skill_sheet": {"name": "s"},
            "_last_seen": now - 10,
            "_announced_at": now - 100,
            "_load": 2
        },
        {
            "node_id": "p_b",
            "host": "h", "port": 0,
            "skill_sheet": {"name": "s"},
            "_last_seen": now - 10,
            "_announced_at": now - 10,
            "_load": 8
        }
    ]
    
    ranked = node._rank_results(results)
    # Freshness weight is 0.2, Availability (load) weight is 0.3.
    # p_a: availability = 0.8 * 0.3 = 0.24. freshness = 0.0 * 0.2 = 0.0.
    # p_b: availability = 0.2 * 0.3 = 0.06. freshness = 1.0 * 0.2 = 0.2.
    # p_a score (partial) = 0.24. p_b score (partial) = 0.26.
    # Wait, liveness is same. Balance is same.
    # So p_b should win due to freshness?
    # Wait, 0.24 vs 0.26. Yes.
    assert ranked[0]["node_id"] == "p_b"

def test_ranking_single_result_unchanged():
    node = DHTNode("127.0.0.1", 0)
    results = [{"node_id": "p1", "skill_sheet": {"name": "s"}}]
    ranked = node._rank_results(list(results))
    assert len(ranked) == 1
    assert ranked[0]["node_id"] == "p1"
