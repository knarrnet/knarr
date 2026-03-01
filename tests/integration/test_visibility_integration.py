import pytest
import asyncio
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_private_skill_not_discoverable():
    provider = DHTNode("127.0.0.1", 0)
    await provider.start()
    
    # Register private skill
    provider.register_handler("private-skill", lambda d: d)
    provider._skill_visibility["private-skill"] = "private"
    await provider.announce({
        "name": "private-skill", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}
    })
    
    consumer = DHTNode("127.0.0.1", 0)
    await consumer.start()
    await consumer.join([f"127.0.0.1:{provider.node_info.port}"])
    
    try:
        results = await consumer.query("name", "private-skill")
        assert len(results) == 0
    finally:
        await consumer.stop()
        await provider.stop()

@pytest.mark.asyncio
async def test_whitelist_skill_discoverable_but_restricted():
    provider = DHTNode("127.0.0.1", 0)
    await provider.start()
    
    # Authorized consumer ID
    consumer_authorized = DHTNode("127.0.0.1", 0)
    await consumer_authorized.start()
    
    provider.register_handler("locked", lambda d: d)
    provider._skill_visibility["locked"] = "whitelist"
    provider._skill_allowed_nodes["locked"] = [consumer_authorized.node_info.node_id]
    
    await provider.announce({
        "name": "locked", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}
    })
    
    consumer_unauthorized = DHTNode("127.0.0.1", 0)
    await consumer_unauthorized.start()
    
    await consumer_authorized.join([f"127.0.0.1:{provider.node_info.port}"])
    await consumer_unauthorized.join([f"127.0.0.1:{provider.node_info.port}"])
    
    try:
        # Both can see it
        res_a = await consumer_authorized.query("name", "locked")
        res_u = await consumer_unauthorized.query("name", "locked")
        assert len(res_a) == 1
        assert len(res_u) == 1
        
        # Authorized succeeds
        task_a = await consumer_authorized.request_task(
            provider.node_info.node_id, "127.0.0.1", provider.node_info.port,
            "locked", {}
        )
        assert task_a.status == "completed"
        
        # Unauthorized fails with ACCESS_DENIED
        task_u = await consumer_unauthorized.request_task(
            provider.node_info.node_id, "127.0.0.1", provider.node_info.port,
            "locked", {}
        )
        assert task_u.status == "failed"
        assert task_u.error["code"] == "ACCESS_DENIED"
        
    finally:
        await consumer_authorized.stop()
        await consumer_unauthorized.stop()
        await provider.stop()
