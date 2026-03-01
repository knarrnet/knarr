import pytest
import asyncio
import time
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_announce_gossip_three_nodes():
    # A <-> B <-> C
    node_a = DHTNode("127.0.0.1", 9600)
    node_b = DHTNode("127.0.0.1", 9601)
    node_c = DHTNode("127.0.0.1", 9602)
    
    await node_a.start()
    await node_b.start()
    await node_c.start()
    
    try:
        await node_b.join(["127.0.0.1:9600"])
        await node_c.join(["127.0.0.1:9601"])
        
        # A announces a skill
        skill_data = {
            "name": "gossip-skill",
            "version": "1.0.0",
            "description": "test",
            "tags": ["nlp"],
            "input_schema": {},
            "output_schema": {}
        }
        await node_a.announce(skill_data)
        
        # Give some time for gossip to reach C
        await asyncio.sleep(1.0)
        
        # C should find it
        results = await node_c.query("name", "gossip-skill")
        assert len(results) == 1
        assert results[0]["node_id"] == node_a.node_info.node_id
        
    finally:
        await node_a.stop()
        await node_b.stop()
        await node_c.stop()

@pytest.mark.asyncio
async def test_startup_sync():
    node_a = DHTNode("127.0.0.1", 9610)
    await node_a.start()
    
    try:
        # A announces a skill
        await node_a.announce({
            "name": "sync-skill",
            "version": "1.0.0",
            "description": "test",
            "tags": ["sync"],
            "input_schema": {},
            "output_schema": {}
        })
        
        # Node B joins LATER
        node_b = DHTNode("127.0.0.1", 9611)
        await node_b.start()
        await node_b.join(["127.0.0.1:9610"])
        
        # B should have discovered it via sync during join
        results = await node_b.query("name", "sync-skill")
        assert len(results) == 1
        assert results[0]["node_id"] == node_a.node_info.node_id
        
        await node_b.stop()
    finally:
        await node_a.stop()

@pytest.mark.asyncio
async def test_deregister_gossip_three_nodes():
    node_a = DHTNode("127.0.0.1", 9620)
    node_b = DHTNode("127.0.0.1", 9621)
    node_c = DHTNode("127.0.0.1", 9622)
    
    await node_a.start()
    await node_b.start()
    await node_c.start()
    
    try:
        await node_b.join(["127.0.0.1:9620"])
        await node_c.join(["127.0.0.1:9621"])
        
        await node_a.announce({
            "name": "bye-skill",
            "version": "1.0.0",
            "description": "test",
            "tags": ["nlp"],
            "input_schema": {},
            "output_schema": {}
        })
        await asyncio.sleep(0.5)
        assert len(await node_c.query("name", "bye-skill")) == 1
        
        # A deregisters
        await node_a.deregister("bye-skill")
        await asyncio.sleep(1.0)
        
        # C should no longer find it
        assert len(await node_c.query("name", "bye-skill")) == 0
        
    finally:
        await node_a.stop()
        await node_b.stop()
        await node_c.stop()
