import pytest
import os
import time
from knarr.dht.storage import Storage
from knarr.core.models import NodeInfo, SkillSheet

@pytest.fixture
def storage():
    s = Storage(":memory:")
    return s

def test_peer_persistence(storage):
    node = NodeInfo(node_id="node1", host="localhost", port=8000)
    storage.upsert_peer(node)
    peers = storage.get_peers()
    assert len(peers) == 1
    assert peers[0].node_id == "node1"

def test_skill_persistence(storage):
    node = NodeInfo(node_id="node1", host="localhost", port=8000)
    storage.upsert_peer(node)
    
    sheet = SkillSheet(
        name="test", version="1.0.0", description="d", tags=["t1"],
        input_schema={}, output_schema={}
    )
    storage.upsert_skill("test", "node1", sheet, provider_public_key="pub", announce_signature="sig", provider_msg_id="msg1")
    
    results = storage.query_skills_by_name("test")
    assert len(results) == 1
    assert results[0]["node_id"] == "node1"
    assert results[0]["skill_sheet"]["name"] == "test"
    
    all_skills = storage.get_all_skills()
    assert len(all_skills) == 1
    assert all_skills[0]["public_key"] == "pub"
    assert all_skills[0]["signature"] == "sig"
    assert all_skills[0]["msg_id"] == "msg1"

def test_skill_query_by_tag(storage):
    node = NodeInfo(node_id="node1", host="localhost", port=8000)
    storage.upsert_peer(node)
    storage.touch_peer("node1") # Ensure it's not stale
    
    sheet = SkillSheet(
        name="test", version="1.0.0", description="d", tags=["target"],
        input_schema={}, output_schema={}
    )
    storage.upsert_skill("test", "node1", sheet)
    
    results = storage.query_skills_by_tag("target")
    assert len(results) == 1
    assert "target" in results[0]["skill_sheet"]["tags"]

def test_prune_stale_skills(storage):
    node = NodeInfo(node_id="node1", host="localhost", port=8000)
    storage.upsert_peer(node)
    
    sheet = SkillSheet(
        name="test", version="1.0.0", description="d", tags=["t"],
        input_schema={}, output_schema={}
    )
    storage.upsert_skill("test", "node1", sheet, ttl=0)
    time.sleep(0.1)
    storage.prune_stale_skills()
    results = storage.query_skills_by_name("test")
    assert len(results) == 0