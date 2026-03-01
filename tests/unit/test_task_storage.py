import pytest
import time
from knarr.dht.storage import Storage
from knarr.core.models import Task

@pytest.fixture
def storage():
    return Storage(":memory:")

def test_task_storage_lifecycle(storage):
    task = Task(
        task_id="t1",
        skill_name="s",
        requester_node_id="r",
        provider_node_id="p",
        status="submitted",
        input_data={"i": 1},
        created_at=time.time(),
        updated_at=time.time()
    )
    storage.insert_task(task)
    
    saved = storage.get_task("t1")
    assert saved is not None
    assert saved.status == "submitted"
    
    storage.update_task_status("t1", "completed", output_data={"o": 2})
    updated = storage.get_task("t1")
    assert updated.status == "completed"
    assert updated.output_data == {"o": 2}

def test_task_pruning(storage):
    task = Task(
        task_id="t1",
        skill_name="s",
        requester_node_id="r",
        provider_node_id="p",
        status="completed",
        input_data={},
        created_at=time.time(),
        updated_at=time.time() - 400 # Old
    )
    storage.insert_task(task)
    
    storage.prune_completed_tasks(max_age=300)
    assert storage.get_task("t1") is None
