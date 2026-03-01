import pytest
import time
from knarr.dht.node import DHTNode
from knarr.core.models import NodeInfo, SkillSheet

def test_node_get_status():
    node = DHTNode("127.0.0.1", 9000, storage_path=":memory:")
    # Mock start time for uptime calculation
    node._start_time = time.monotonic() - 10
    
    status = node.get_status()
    assert "node_id" in status
    assert status["version"].startswith("0.")
    assert status["uptime_seconds"] >= 10
    assert status["peer_count"] == 0
    assert status["skill_count"] == 0
    assert "task_slots" in status
    assert status["port"] == 9000

def test_node_get_peers():
    node = DHTNode("127.0.0.1", 9000, storage_path=":memory:")
    peer = NodeInfo(node_id="peer1", host="1.2.3.4", port=9001)
    node.storage.upsert_peer(peer)
    
    peers = node.get_peers()
    assert len(peers) == 1
    assert peers[0]["node_id"] == "peer1"
    assert peers[0]["host"] == "1.2.3.4"
    assert peers[0]["port"] == 9001

def test_node_get_skills_empty():
    node = DHTNode("127.0.0.1", 9000, storage_path=":memory:")
    skills = node.get_skills()
    assert skills == {"local": [], "network": []}

def test_node_get_skills_populated():
    node = DHTNode("127.0.0.1", 9000, storage_path=":memory:")
    
    # Add local skill
    async def dummy_handler(data): return {}
    node.register_handler("test-skill", dummy_handler)
    node._handler_specs["test-skill"] = "dummy.py:handle"
    
    # Add network skill
    sheet = SkillSheet(name="net-skill", version="1.0.0", description="desc", tags=["tag1"],
                       input_schema={}, output_schema={})
    node.storage.upsert_skill("net-skill", "peer1", sheet, sidecar_port=8001)
    # Upsert peer to make it active in query_all_active_skills
    node.storage.upsert_peer(NodeInfo(node_id="peer1", host="1.2.3.4", port=9001))
    
    skills = node.get_skills()
    assert len(skills["local"]) == 1
    assert skills["local"][0]["name"] == "test-skill"
    assert skills["local"][0]["handler"] == "dummy.py:handle"
    
    assert len(skills["network"]) == 1
    assert skills["network"][0]["name"] == "net-skill"
    assert len(skills["network"][0]["providers"]) == 1
    assert skills["network"][0]["providers"][0]["node_id"] == "peer1"
    assert skills["network"][0]["providers"][0]["sidecar_port"] == 8001
