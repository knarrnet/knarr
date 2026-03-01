import pytest
import asyncio
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_two_node_discovery():
    node_a = DHTNode("127.0.0.1", 9000)
    node_b = DHTNode("127.0.0.1", 9001)
    
    await node_a.start()
    await node_b.start()
    
    try:
        # B joins A
        await node_b.join(["127.0.0.1:9000"])
        
        # A announces a skill
        skill_data = {
            "name": "test-skill",
            "version": "1.0.0",
            "description": "test",
            "tags": ["tag1"],
            "input_schema": {},
            "output_schema": {}
        }
        await node_a.announce(skill_data)
        
        # Small delay for replication (background tasks)
        # Actually announce pushes to all known peers immediately.
        await asyncio.sleep(0.2)
        
        # B queries for the skill
        results = await node_b.query("name", "test-skill")
        assert len(results) >= 1
        assert results[0]["node_id"] == node_a.node_info.node_id
        assert results[0]["skill_sheet"]["name"] == "test-skill"
        
        # B queries by tag
        tag_results = await node_b.query("tag", "tag1")
        assert len(tag_results) >= 1
        assert tag_results[0]["skill_sheet"]["name"] == "test-skill"
        
    finally:
        await node_a.stop()
        await node_b.stop()
