from knarr.dht.mcp_bridge import MCPBridge
from knarr.dht.node import DHTNode

def test_tool_filter_allow():
    node = DHTNode("127.0.0.1", 8000)
    bridge = MCPBridge(["mock"], node, tool_filter={"allow": ["t1"]})
    tools = [{"name": "t1"}, {"name": "t2"}]
    filtered = bridge._apply_filter(tools)
    assert len(filtered) == 1
    assert filtered[0]["name"] == "t1"

def test_tool_filter_deny():
    node = DHTNode("127.0.0.1", 8000)
    bridge = MCPBridge(["mock"], node, tool_filter={"deny": ["t1"]})
    tools = [{"name": "t1"}, {"name": "t2"}]
    filtered = bridge._apply_filter(tools)
    assert len(filtered) == 1
    assert filtered[0]["name"] == "t2"

def test_tool_filter_both():
    node = DHTNode("127.0.0.1", 8000)
    bridge = MCPBridge(["mock"], node, tool_filter={"allow": ["t1", "t2"], "deny": ["t1"]})
    tools = [{"name": "t1"}, {"name": "t2"}, {"name": "t3"}]
    filtered = bridge._apply_filter(tools)
    assert len(filtered) == 1
    assert filtered[0]["name"] == "t2"
