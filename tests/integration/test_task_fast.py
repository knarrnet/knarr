import pytest
import asyncio
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_task_fast_path():
    node_a = DHTNode("127.0.0.1", 9100)
    node_b = DHTNode("127.0.0.1", 9101)
    
    await node_a.start()
    await node_b.start()
    
    try:
        await node_b.join(["127.0.0.1:9100"])
        
        async def fast_handler(data):
            return {"result": data["x"] * 2}
            
        node_a.register_handler("double", fast_handler, slow=False)
        await node_a.announce({
            "name": "double",
            "version": "1.0.0",
            "description": "doubles x",
            "tags": ["math"],
            "input_schema": {"x": "int"},
            "output_schema": {"result": "int"}
        })
        
        await asyncio.sleep(0.1)
        
        # B requests task from A
        res = await node_b.request_task(
            node_a.node_info.node_id, "127.0.0.1", 9100,
            "double", {"x": 21}
        )
        
        assert res.status == "completed"
        assert res.output_data["result"] == 42
        
    finally:
        await node_a.stop()
        await node_b.stop()

@pytest.mark.asyncio
async def test_task_unknown_skill():
    node_a = DHTNode("127.0.0.1", 9110)
    node_b = DHTNode("127.0.0.1", 9111)
    await node_a.start()
    await node_b.start()
    try:
        await node_b.join(["127.0.0.1:9110"])
        res = await node_b.request_task(
            node_a.node_info.node_id, "127.0.0.1", 9110,
            "none", {}
        )
        assert res.status == "failed"
        assert res.error["code"] == "UNKNOWN_SKILL"
    finally:
        await node_a.stop()
        await node_b.stop()