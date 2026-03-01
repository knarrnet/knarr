import pytest
import asyncio
import sys
import os
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_mcp_bridge_startup_and_call():
    node = DHTNode("127.0.0.1", 9300)
    await node.start()
    
    # Path to mock server
    mock_server_path = os.path.join(os.path.dirname(__file__), "..", "fixtures", "mock_mcp_server.py")
    
    try:
        bridge = await node.start_mcp_bridge([sys.executable, mock_server_path])
        
        # Verify tools announced as skills
        # DHTNode.announce is called, so it should be in _own_skills
        assert "echo" in node._own_skills
        assert "add-numbers" in node._own_skills
        
        # Call a tool via request_task
        res = await node.request_task(
            node.node_info.node_id, "127.0.0.1", 9300,
            "add-numbers", {"a": 10, "b": 32}
        )
        assert res.status == "completed"
        assert res.output_data["content"] == "42.0"
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_mcp_error_handling():
    node = DHTNode("127.0.0.1", 9310)
    await node.start()
    mock_server_path = os.path.join(os.path.dirname(__file__), "..", "fixtures", "mock_mcp_server.py")
    
    try:
        await node.start_mcp_bridge([sys.executable, mock_server_path])
        
        res = await node.request_task(
            node.node_info.node_id, "127.0.0.1", 9310,
            "error-tool", {}
        )
        assert res.status == "failed"
        assert res.error["code"] == "HANDLER_ERROR"
        assert res.error["message"] == "Handler execution failed"
        
    finally:
        await node.stop()
