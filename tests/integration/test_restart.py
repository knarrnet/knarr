import pytest
import asyncio
import os
import tempfile
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_restart_survival():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        # Node A uses persistent storage
        node_a = DHTNode("127.0.0.1", 9030, storage_path=db_path)
        await node_a.start()
        
        # A announces a skill
        skill_data = {
            "name": "persistent-skill",
            "version": "1.0.0",
            "description": "test",
            "tags": ["tag"],
            "input_schema": {},
            "output_schema": {}
        }
        await node_a.announce(skill_data)
        node_a_id = node_a.node_info.node_id
        
        # Node B joins
        node_b = DHTNode("127.0.0.1", 9031)
        await node_b.start()
        await node_b.join(["127.0.0.1:9030"])
        
        # Verify B has it
        assert len(await node_b.query("name", "persistent-skill")) == 1
        
        # Restart A
        await node_a.stop()
        node_a_new = DHTNode("127.0.0.1", 9030, storage_path=db_path)
        
        # Node ID should be the same
        assert node_a_new.node_info.node_id == node_a_id
        
        await node_a_new.start()
        # It should have reloaded its skill
        assert "persistent-skill" in node_a_new._own_skills
        
        # Wait for B to notice A is back (B will query A eventually or A re-announces on join)
        # For Phase 1, query() checks network, so B querying should find A.
        results = await node_b.query("name", "persistent-skill")
        assert len(results) == 1
        
        await node_a_new.stop()
        await node_b.stop()
        
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)