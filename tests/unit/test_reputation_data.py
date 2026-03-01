import time
import pytest
from knarr.dht.storage import Storage
from knarr.core.models import NodeInfo, SkillSheet, Task

@pytest.fixture
def storage():
    return Storage(":memory:")

def test_query_returns_provider_public_key(storage):
    # Announce a skill with provider_public_key set
    sheet = SkillSheet("test-skill", "1.0.0", "d", ["t"], {}, {})
    storage.upsert_skill(
        "test-skill", "node1", sheet,
        provider_public_key="pub1",
        announce_signature="sig1",
        provider_msg_id="msg1"
    )
    # Add peer record so join works
    storage.upsert_peer(NodeInfo("node1", "127.0.0.1", 9000))
    
    results = storage.query_skills_by_name("test-skill")
    assert len(results) == 1
    assert results[0]["_provider_public_key"] == "pub1"

def test_query_by_tag_returns_provider_public_key(storage):
    sheet = SkillSheet("test-skill", "1.0.0", "d", ["t"], {}, {})
    storage.upsert_skill(
        "test-skill", "node1", sheet,
        provider_public_key="pub1"
    )
    storage.upsert_peer(NodeInfo("node1", "127.0.0.1", 9000))
    
    results = storage.query_skills_by_tag("t")
    assert len(results) == 1
    assert results[0]["_provider_public_key"] == "pub1"

def test_reputation_basic(storage):
    # Insert 5 tasks: 4 completed, 1 failed
    for i in range(4):
        t = Task(f"t{i}", "s", "r", "p1", "completed", {})
        storage.insert_task(t)
        storage.update_task_status(f"t{i}", "completed", wall_time_ms=100)
    
    storage.insert_task(Task("t4", "s", "r", "p1", "failed", {}))
    storage.update_task_status("t4", "failed")
    
    rep = storage.get_provider_reputation("p1")
    assert rep["total_tasks"] == 5
    assert rep["completed"] == 4
    assert rep["failed"] == 1
    assert rep["success_rate"] == 0.8

def test_reputation_window(storage):
    now = time.time()
    # Task inside window
    storage.insert_task(Task("t1", "s", "r", "p1", "completed", {}))
    storage.update_task_status("t1", "completed", wall_time_ms=100)
    
    # Task outside window (45 days ago)
    storage.insert_task(Task("t2", "s", "r", "p1", "completed", {}))
    # Manually update updated_at in DB
    storage._get_conn().execute("UPDATE tasks SET updated_at = ? WHERE task_id = 't2'", (now - 45*86400,))
    storage._get_conn().commit()
    
    rep = storage.get_provider_reputation("p1", window_days=30)
    assert rep["total_tasks"] == 1
    assert rep["completed"] == 1

def test_reputation_per_skill(storage):
    storage.insert_task(Task("t1", "skill1", "r", "p1", "completed", {}))
    storage.update_task_status("t1", "completed", wall_time_ms=100)
    
    storage.insert_task(Task("t2", "skill2", "r", "p1", "completed", {}))
    storage.update_task_status("t2", "completed", wall_time_ms=500)
    
    rep1 = storage.get_provider_reputation("p1", skill_name="skill1")
    assert rep1["total_tasks"] == 1
    assert rep1["avg_wall_time_ms"] == 100
    
    rep2 = storage.get_provider_reputation("p1", skill_name="skill2")
    assert rep2["total_tasks"] == 1
    assert rep2["avg_wall_time_ms"] == 500

def test_counterparty_count(storage):
    storage.get_or_create_ledger_entry("key1")
    storage.get_or_create_ledger_entry("key2")
    storage.get_or_create_ledger_entry("key3")
    assert storage.get_counterparty_count() == 3

def test_reputation_empty(storage):
    rep = storage.get_provider_reputation("unknown")
    assert rep["total_tasks"] == 0
    assert rep["success_rate"] is None

def test_all_provider_reputations(storage):
    storage.insert_task(Task("t1", "s", "r", "p1", "completed", {}))
    storage.update_task_status("t1", "completed")
    storage.insert_task(Task("t2", "s", "r", "p2", "completed", {}))
    storage.update_task_status("t2", "completed")
    storage.insert_task(Task("t3", "s", "r", "p3", "failed", {}))
    storage.update_task_status("t3", "failed")
    
    reps = storage.get_all_provider_reputations()
    assert len(reps) == 3
    nodes = {r["provider_node_id"] for r in reps}
    assert nodes == {"p1", "p2", "p3"}

def test_insert_task_with_provider_public_key(storage):
    t = Task("t1", "s", "r", "p1", "submitted", {})
    storage.insert_task(t, provider_public_key="pub1")
    
    conn = storage._get_conn()
    row = conn.execute("SELECT provider_public_key FROM tasks WHERE task_id = 't1'").fetchone()
    assert row[0] == "pub1"

def test_provider_public_key_migration(storage):
    # Triggers _init_db
    conn = storage._get_conn()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "provider_public_key" in columns
