import pytest
import asyncio
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_task_handler_error():
    node_a = DHTNode("127.0.0.1", 9130)
    node_b = DHTNode("127.0.0.1", 9131)
    await node_a.start()
    await node_b.start()
    try:
        await node_b.join(["127.0.0.1:9130"])
        
        async def error_handler(data):
            raise ValueError("boom")
            
        node_a.register_handler("fail", error_handler)
        await node_a.announce({
            "name": "fail",
            "version": "1.0.0",
            "description": "fails",
            "tags": ["error"],
            "input_schema": {},
            "output_schema": {}
        })
        
        await asyncio.sleep(0.1)
        
        res = await node_b.request_task(
            node_a.node_info.node_id, "127.0.0.1", 9130,
            "fail", {}
        )
        assert res.status == "failed"
        assert res.error["code"] == "HANDLER_ERROR"
        assert res.error["message"] == "Handler execution failed"
    finally:
        await node_a.stop()
        await node_b.stop()

@pytest.mark.asyncio
async def test_task_invalid_input():
    node_a = DHTNode("127.0.0.1", 9140)
    node_b = DHTNode("127.0.0.1", 9141)
    await node_a.start()
    await node_b.start()
    try:
        await node_b.join(["127.0.0.1:9140"])
        
        async def mock_handler(d):
            return d
            
        node_a.register_handler("check", mock_handler)
        await node_a.announce({
            "name": "check",
            "version": "1.0.0",
            "description": "d",
            "tags": ["test"],
            "input_schema": {"required": "str"},
            "output_schema": {}
        })
        
        await asyncio.sleep(0.1)
        
        res = await node_b.request_task(
            node_a.node_info.node_id, "127.0.0.1", 9140,
            "check", {"wrong": 1}
        )
        assert res.status == "failed"
        assert res.error["code"] == "INVALID_INPUT"
        assert "Missing required fields" in res.error["message"]
    finally:
        await node_a.stop()
        await node_b.stop()
