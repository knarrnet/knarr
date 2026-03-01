import pytest
import asyncio
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_task_timeout():
    node_a = DHTNode("127.0.0.1", 9150)
    node_b = DHTNode("127.0.0.1", 9151)
    await node_a.start()
    await node_b.start()
    try:
        await node_b.join(["127.0.0.1:9150"])
        
        async def slow_handler(data):
            await asyncio.sleep(2.0)
            return {}
            
        node_a.register_handler("too-slow", slow_handler, slow=True)
        await node_a.announce({
            "name": "too-slow",
            "version": "1.0.0",
            "description": "d",
            "tags": ["slow"],
            "input_schema": {},
            "output_schema": {}
        })
        
        await asyncio.sleep(0.1)
        
        # B requests with 500ms timeout, but handler takes 2s
        res = await node_b.request_task(
            node_a.node_info.node_id, "127.0.0.1", 9150,
            "too-slow", {}, timeout_ms=500
        )
        assert res.status == "failed"
        assert res.error["code"] == "TIMEOUT"
    finally:
        await node_a.stop()
        await node_b.stop()