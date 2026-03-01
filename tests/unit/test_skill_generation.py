from knarr.dht.mcp_bridge import MCPBridge
from knarr.dht.node import DHTNode

def test_skill_generation():
    node = DHTNode("127.0.0.1", 8000)
    bridge = MCPBridge(["mock"], node)
    
    tool = {
        "name": "get_weather_data",
        "description": "Returns weather for a location",
        "inputSchema": {
            "type": "object",
            "properties": {"location": {"type": "string"}}
        }
    }
    
    skill = bridge._generate_skill_sheet(tool)
    assert skill["name"] == "get-weather-data"
    assert skill["description"] == "Returns weather for a location"
    assert "mcp" in skill["tags"]
    assert skill["input_schema"] == {"location": "string"}
    assert skill["input_schema_full"] == tool["inputSchema"]

def test_skill_generation_truncation():
    node = DHTNode("127.0.0.1", 8000)
    bridge = MCPBridge(["mock"], node)
    
    tool = {
        "name": "long",
        "description": "a" * 500
    }
    skill = bridge._generate_skill_sheet(tool)
    assert len(skill["description"]) == 500  # under 1024 limit, no truncation
