import pytest
import asyncio
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_task_e2e_discovery_and_execution():
    """
    End-to-end test: Node A announces a skill, Node B discovers it via QUERY,
    and then Node B requests task execution from Node A.
    """
    node_a = DHTNode("127.0.0.1", 9200)
    node_b = DHTNode("127.0.0.1", 9201)
    
    await node_a.start()
    await node_b.start()
    
    try:
        # B joins A
        await node_b.join(["127.0.0.1:9200"])
        
        # A registers and announces a skill
        async def reverse_handler(data):
            return {"reversed": data["text"][::-1]}
            
        node_a.register_handler("reverse", reverse_handler)
        await node_a.announce({
            "name": "reverse",
            "version": "1.0.0",
            "description": "reverses text",
            "tags": ["nlp", "utility"],
            "input_schema": {"text": "string"},
            "output_schema": {"reversed": "string"}
        })
        
        # B discovers the skill by name
        discovery_results = await node_b.query("name", "reverse")
        assert len(discovery_results) == 1
        provider_info = discovery_results[0]
        assert provider_info["node_id"] == node_a.node_info.node_id
        
        # B requests task execution using discovered info
        res = await node_b.request_task(
            provider_node_id=provider_info["node_id"],
            provider_host=provider_info["host"],
            provider_port=provider_info["port"],
            skill_name="reverse",
            input_data={"text": "knarr"}
        )
        
        assert res.status == "completed"
        assert res.output_data["reversed"] == "rrank"
        
    finally:
        await node_a.stop()
        await node_b.stop()
