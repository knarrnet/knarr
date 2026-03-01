import pytest
import asyncio
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_task_slow_path():
    node_a = DHTNode("127.0.0.1", 9120)
    node_b = DHTNode("127.0.0.1", 9121)
    
    await node_a.start()
    await node_b.start()
    
    try:
        await node_b.join(["127.0.0.1:9120"])
        
        async def slow_handler(data):
            await asyncio.sleep(0.5)
            return {"echo": data["msg"]}
            
        node_a.register_handler("echo-slow", slow_handler, slow=True)
        await node_a.announce({
            "name": "echo-slow",
            "version": "1.0.0",
            "description": "echoes after delay",
            "tags": ["test"],
            "input_schema": {"msg": "string"},
            "output_schema": {"echo": "string"}
        })
        
        await asyncio.sleep(0.1)
        
        # B requests task from A
        res = await node_b.request_task(
            node_a.node_info.node_id, "127.0.0.1", 9120,
            "echo-slow", {"msg": "hello"}
        )
        
        assert res.status == "completed"
        assert res.output_data["echo"] == "hello"
        
    finally:
        await node_a.stop()
        await node_b.stop()
