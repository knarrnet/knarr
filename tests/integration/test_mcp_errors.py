import pytest
import asyncio
import sys
import os
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_mcp_crash_handling():
    node = DHTNode("127.0.0.1", 9320)
    await node.start()
    mock_server_path = os.path.join(os.path.dirname(__file__), "..", "fixtures", "mock_mcp_server.py")
    
    try:
        # Start bridge with --crash-on-call
        await node.start_mcp_bridge([sys.executable, mock_server_path, "--crash-on-call"])
        
        res = await node.request_task(
            node.node_info.node_id, "127.0.0.1", 9320,
            "echo", {"text": "boom"}
        )
        assert res.status == "failed"
        assert res.error["code"] == "HANDLER_ERROR"
        # Exception scrubbing (SA6) replaces internal error details
        assert res.error["message"] == "Handler execution failed"
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_mcp_timeout_handling():
    node = DHTNode("127.0.0.1", 9330)
    await node.start()
    mock_server_path = os.path.join(os.path.dirname(__file__), "..", "fixtures", "mock_mcp_server.py")
    
    try:
        # Start bridge with --hang-on-call and 1s timeout
        await node.start_mcp_bridge([sys.executable, mock_server_path, "--hang-on-call"], tool_timeout=1.0)
        
        res = await node.request_task(
            node.node_info.node_id, "127.0.0.1", 9330,
            "echo", {"text": "wait"}, timeout_ms=5000
        )
        assert res.status == "failed"
        assert res.error["code"] == "TIMEOUT"
        
    finally:
        await node.stop()
