import asyncio
import pytest
import time
import sys
from unittest.mock import patch, MagicMock

# Mock knarr.mail.tls
mock_tls = MagicMock()
mock_tls.resolve_cert_paths = MagicMock(return_value=("cert.pem", "key.pem"))
sys.modules["knarr.mail.tls"] = mock_tls

from knarr.dht.node import DHTNode
from knarr.core.messages import TaskRequest

@pytest.mark.asyncio
async def test_job_id_propagates_through_chain():
    node = DHTNode("127.0.0.1", 0)
    
    # Handler B
    async def handler_b(input_data):
        return {"result": "B", "job_id": input_data.get("_job_id")}
    
    # Handler A calls B
    async def handler_a(input_data):
        # Explicitly propagate _job_id if present
        b_input = {"val": 1}
        if "_job_id" in input_data:
            b_input["_job_id"] = input_data["_job_id"]
        res = await node.call_local("skill_b", b_input)
        return {"result": "A", "b_result": res}
    
    node.register_handler("skill_a", handler_a)
    node.register_handler("skill_b", handler_b)
    
    with patch("os.path.exists", return_value=True):
        await node.start()
    
    try:
        # Initial call without _job_id
        res = await node.call_local("skill_a", {})
        
        # Verify job_id propagated to B
        b_job_id = res["b_result"]["job_id"]
        assert b_job_id is not None
        
        # Check execution log entries
        logs = node.storage.get_execution_log(job_id=b_job_id)
        # There should be at least two entries with same job_id (one for A, one for B)
        # Wait for background writes
        await asyncio.sleep(0.1)
        logs = node.storage.get_execution_log(job_id=b_job_id)
        assert len(logs) == 2
        assert logs[0]["job_id"] == b_job_id
        assert logs[1]["job_id"] == b_job_id
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_execution_log_records_success():
    node = DHTNode("127.0.0.1", 0)
    node.register_handler("test", lambda d: {"ok": True})
    await node.start()
    
    try:
        await node.call_local("test", {"x": 1})
        await asyncio.sleep(0.1)
        
        logs = node.storage.get_execution_log(skill="test")
        assert len(logs) == 1
        assert logs[0]["status"] == "completed"
        assert logs[0]["wall_time_ms"] >= 0
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_execution_log_records_failure():
    node = DHTNode("127.0.0.1", 0)
    def fail_handler(d):
        raise ValueError("Boom")
    node.register_handler("fail", fail_handler)
    await node.start()
    
    try:
        with pytest.raises(ValueError):
            await node.call_local("fail", {})
        
        await asyncio.sleep(0.1)
        logs = node.storage.get_execution_log(skill="fail")
        assert len(logs) == 1
        assert logs[0]["status"] == "failed"
        assert "Boom" in logs[0]["error"]
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_call_local_records_task():
    node = DHTNode("127.0.0.1", 0)
    node.register_handler("test", lambda d: {"ok": True})
    await node.start()
    
    try:
        res = await node.call_local("test", {"x": 1})
        await asyncio.sleep(0.1)
        
        tasks = node.get_tasks()
        # Find the task for this execution
        # Since it's a fresh node, it should be the only one or we filter by skill
        test_tasks = [t for t in tasks if t["skill_name"] == "test"]
        assert len(test_tasks) == 1
        assert test_tasks[0]["status"] == "completed"
    finally:
        await node.stop()
