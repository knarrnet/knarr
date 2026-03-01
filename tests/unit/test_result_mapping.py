from knarr.dht.mcp_bridge import MCPBridge
from knarr.dht.node import DHTNode
import pytest

def test_result_mapping_text():
    node = DHTNode("127.0.0.1", 8000)
    bridge = MCPBridge(["mock"], node)
    mcp_result = {
        "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}],
        "isError": False
    }
    mapped = bridge._map_result(mcp_result)
    assert mapped["content"] == "hello\nworld"

def test_result_mapping_mixed():
    node = DHTNode("127.0.0.1", 8000)
    bridge = MCPBridge(["mock"], node)
    mcp_result = {
        "content": [
            {"type": "text", "text": "image follows"},
            {"type": "image", "data": "base64..."}
        ],
        "isError": False
    }
    mapped = bridge._map_result(mcp_result)
    assert mapped["content"] == "image follows"
    assert len(mapped["attachments"]) == 1
    assert mapped["attachments"][0]["type"] == "image"

def test_result_mapping_error():
    node = DHTNode("127.0.0.1", 8000)
    bridge = MCPBridge(["mock"], node)
    mcp_result = {
        "content": [{"type": "text", "text": "failed"}],
        "isError": True
    }
    from knarr.dht.mcp_bridge import MCPToolError
    with pytest.raises(MCPToolError, match="failed"):
        bridge._map_result(mcp_result)