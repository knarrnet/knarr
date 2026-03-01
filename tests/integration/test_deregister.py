import pytest
import asyncio
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_deregister_flow():
    node_a = DHTNode("127.0.0.1", 9020)
    node_b = DHTNode("127.0.0.1", 9021)
    
    await node_a.start()
    await node_b.start()
    
    try:
        await node_b.join(["127.0.0.1:9020"])
        
        skill_data = {
            "name": "temp-skill",
            "version": "1.0.0",
            "description": "test",
            "tags": ["tag"],
            "input_schema": {},
            "output_schema": {}
        }
        await node_a.announce(skill_data)
        await asyncio.sleep(0.2)
        
        # Verify it exists
        results = await node_b.query("name", "temp-skill")
        assert len(results) == 1
        
        # Deregister
        await node_a.deregister("temp-skill")
        await asyncio.sleep(0.2)
        
        # Verify it's gone
        results = await node_b.query("name", "temp-skill")
        assert len(results) == 0
        
    finally:
        await node_a.stop()
        await node_b.stop()
