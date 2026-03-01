import asyncio
import time
import pytest
from knarr.dht.node import DHTNode
from knarr.core.messages import TaskRequest

@pytest.mark.asyncio
async def test_task_records_input_size_and_wall_time():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    async def slow_handler(data):
        await asyncio.sleep(0.2)
        return {"out": "ok"}
        
    node.register_handler("telemetry", slow_handler)
    await node.announce({
        "name": "telemetry", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {"in": "str"}, "output_schema": {"out": "str"}
    })
    
    try:
        input_data = {"in": "hello world"}
        req = node._sign(TaskRequest(
            task_id="t_telemetry", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999,
            skill_name="telemetry", input_data=input_data
        ))
        
        await node._process_message(req)
        
        # Verify columns in DB
        task = node.storage.get_task("t_telemetry")
        # I need to use a raw query because Storage.get_task doesn't include the new columns yet in its return type
        conn = node.storage._get_conn()
        row = conn.execute("SELECT input_size_bytes, wall_time_ms FROM tasks WHERE task_id = 't_telemetry'").fetchone()
        
        assert row[0] > 0 # input_size_bytes
        assert row[1] >= 200 # wall_time_ms (>= 200ms)
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_skill_task_stats_aggregation():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    async def handler(data):
        await asyncio.sleep(data["delay"])
        return {"out": "ok"}
        
    node.register_handler("stats", handler)
    await node.announce({
        "name": "stats", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {"delay": "float"}, "output_schema": {"out": "str"}
    })
    
    try:
        # Complete 3 tasks
        for i, delay in enumerate([0.1, 0.2, 0.3]):
            req = node._sign(TaskRequest(
                task_id=f"ts_{i}", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999,
                skill_name="stats", input_data={"delay": delay}
            ))
            await node._process_message(req)
            
        stats = node.storage.get_skill_task_stats("stats")
        assert stats["total_completed"] == 3
        assert 150 <= stats["avg_wall_time_ms"] <= 250 # avg of 100, 200, 300 is 200
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_recent_tasks_ordering():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    node.register_handler("recent", lambda d: {"out": "ok"})
    await node.announce({
        "name": "recent", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}
    })
    
    try:
        for i in range(5):
            req = node._sign(TaskRequest(
                task_id=f"tr_{i}", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999,
                skill_name="recent", input_data={}
            ))
            await node._process_message(req)
            await asyncio.sleep(0.01) # ensure diff created_at
            
        recent = node.storage.get_recent_tasks(3)
        assert len(recent) == 3
        # Should be tr_4, tr_3, tr_2
        assert recent[0]["task_id"] == "tr_4"
        assert recent[1]["task_id"] == "tr_3"
        assert recent[2]["task_id"] == "tr_2"
        
    finally:
        await node.stop()
