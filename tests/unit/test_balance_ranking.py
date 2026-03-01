import time
import pytest
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_balance_wired_into_ranking():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    try:
        now = time.time()
        # Mock ledger balances
        node.storage.get_or_create_ledger_entry("pub_high", 50.0)
        node.storage.get_or_create_ledger_entry("pub_low", 10.0)
        
        results = [
            {
                "node_id": "p_low",
                "host": "h", "port": 0,
                "skill_sheet": {"name": "s"},
                "_last_seen": now - 10,
                "_announced_at": now - 10,
                "_load": 0,
                "_provider_public_key": "pub_low"
            },
            {
                "node_id": "p_high",
                "host": "h", "port": 0,
                "skill_sheet": {"name": "s"},
                "_last_seen": now - 10,
                "_announced_at": now - 10,
                "_load": 0,
                "_provider_public_key": "pub_high"
            }
        ]
        
        ranked = node._rank_results(results)
        # High balance (50.0) should rank first
        assert ranked[0]["node_id"] == "p_high"
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_balance_zero_for_unknown_provider():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        results = [{
            "node_id": "p1", "skill_sheet": {"name": "s"},
            "_last_seen": time.time(), "_announced_at": time.time(), "_load": 0
        }]
        ranked = node._rank_results(results)
        # Verify it doesn't crash and returns 1 item
        assert len(ranked) == 1
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_balance_none_from_ledger_treated_as_zero():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        # Key not in ledger
        results = [{
            "node_id": "p1", "skill_sheet": {"name": "s"},
            "_last_seen": time.time(), "_announced_at": time.time(), "_load": 0,
            "_provider_public_key": "unknown_pub"
        }]
        ranked = node._rank_results(results)
        assert len(ranked) == 1
    finally:
        await node.stop()
