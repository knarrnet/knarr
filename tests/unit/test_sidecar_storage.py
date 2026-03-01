import pytest
import json
from knarr.dht.storage import Storage
from knarr.core.models import SkillSheet

@pytest.fixture
def storage():
    return Storage(":memory:")

def test_sidecar_port_migration(storage):
    # Verify migration added the column
    conn = storage._get_conn()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(skills)").fetchall()}
    assert "sidecar_port" in columns

def test_upsert_and_query_sidecar_port(storage):
    sheet = SkillSheet("skill1", "1.0", "d", ["t"], {}, {})
    storage.upsert_skill(
        "skill1", "node1", sheet,
        sidecar_port=8080
    )
    # Add peer so query works
    from knarr.core.models import NodeInfo
    storage.upsert_peer(NodeInfo("node1", "127.0.0.1", 9000))
    
    # Query by name
    results = storage.query_skills_by_name("skill1")
    assert len(results) == 1
    assert results[0]["sidecar_port"] == 8080
    
    # Query by tag
    results = storage.query_skills_by_tag("t")
    assert len(results) == 1
    assert results[0]["sidecar_port"] == 8080
    
    # Query all
    results = storage.query_all_active_skills()
    assert len(results) == 1
    assert results[0]["sidecar_port"] == 8080

def test_upsert_default_sidecar_port(storage):
    from knarr.core.models import NodeInfo
    sheet = SkillSheet("skill2", "1.0", "d", [], {}, {})
    storage.upsert_skill("skill2", "node1", sheet)
    storage.upsert_peer(NodeInfo("node1", "127.0.0.1", 9000))
    
    results = storage.query_skills_by_name("skill2")
    assert len(results) == 1
    assert results[0]["sidecar_port"] == 0
