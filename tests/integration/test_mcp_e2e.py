import pytest
import asyncio
import sys
import os
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_mcp_filter():
    node = DHTNode("127.0.0.1", 9340)
    await node.start()
    mock_server_path = os.path.join(os.path.dirname(__file__), "..", "fixtures", "mock_mcp_server.py")
    
    try:
        # Only allow echo
        await node.start_mcp_bridge([sys.executable, mock_server_path], tool_filter={"allow": ["echo"]})
        
        assert "echo" in node._own_skills
        assert "add-numbers" not in node._own_skills
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_mcp_e2e_flow():
    node_a = DHTNode("127.0.0.1", 9350)
    node_b = DHTNode("127.0.0.1", 9351)
    await node_a.start()
    await node_b.start()
    
    mock_server_path = os.path.join(os.path.dirname(__file__), "..", "fixtures", "mock_mcp_server.py")
    
    try:
        await node_b.join(["127.0.0.1:9350"])
        await node_a.start_mcp_bridge([sys.executable, mock_server_path])
        
        # B discovers bridged skill
        results = await node_b.query("name", "echo")
        assert len(results) == 1
        
        # B requests task
        res = await node_b.request_task(
            node_a.node_info.node_id, "127.0.0.1", 9350,
            "echo", {"text": "hello from b"}
        )
        assert res.status == "completed"
        assert res.output_data["content"] == "hello from b"
        
    finally:
        await node_a.stop()
        await node_b.stop()
