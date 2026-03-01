import asyncio
import pytest
import time
from knarr.dht.node import DHTNode
from knarr.core.messages import TaskRequest

@pytest.mark.asyncio
async def test_retry_after_estimation():
    """Fast task gets RETRY_AFTER when workers are saturated (not enqueued)."""
    node = DHTNode("127.0.0.1", 0, config={"node": {"task_slots": 1}})
    await node.start()

    # Register fast handler and populate telemetry
    async def init_handler(data):
        await asyncio.sleep(0.1)
        return {"res": "ok"}

    node.register_handler("test", init_handler)
    await node.announce({
        "name": "test", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}
    })

    # Complete some tasks to get avg_wall_time_ms
    for i in range(3):
        req = node._sign(TaskRequest(task_id=f"init_{i}", skill_name="test", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999))
        await node._process_message(req)

    stats = node.storage.get_skill_task_stats("test")
    assert stats["total_completed"] == 3

    # Register slow handler so the first task occupies the worker
    async def slow_handler(data):
        await asyncio.sleep(0.5)
        return {"res": "ok"}
    node.register_handler("test", slow_handler)

    try:
        # Start one task (saturates the single worker)
        req1 = node._sign(TaskRequest(task_id="t1", skill_name="test", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999))
        t1 = asyncio.create_task(node._process_message(req1))
        await asyncio.sleep(0.1)

        assert node._active_workers == 1

        # Second task (fast): should get RETRY_AFTER immediately
        req2 = node._sign(TaskRequest(task_id="t2", skill_name="test", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999))
        res2 = await node._process_message(req2)

        assert res2.status == "failed"
        assert res2.error["code"] == "RETRY_AFTER"
        assert res2.error["retry_after_seconds"] >= 1

        await t1

    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_provider_busy_when_queue_full():
    """Slow task gets PROVIDER_BUSY when queue is full."""
    # task_slots=1 -> queue_max=2.
    node = DHTNode("127.0.0.1", 0, config={"node": {"task_slots": 1}})
    await node.start()

    async def slow_handler(data):
        await asyncio.sleep(1.0)
        return {"res": "ok"}
    node.register_handler("test", slow_handler, slow=True)
    await node.announce({
        "name": "test", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}
    })

    try:
        # Stop worker loops so they don't dequeue
        for task in node.background_tasks:
            if "task_worker_loop" in str(task):
                task.cancel()
        await asyncio.sleep(0.1)

        # 1 worker busy (simulated)
        node._active_workers = 1

        # Fill queue (2 items = max for task_slots=1)
        async def dummy(d): return {}
        for i in range(2):
            node._task_queue.put_nowait((TaskRequest(task_id=f"m{i}"), dummy, True, 0, 0, asyncio.get_running_loop().create_future()))

        assert node._task_queue.qsize() == 2

        # Next slow task: queue full → PROVIDER_BUSY
        req = node._sign(TaskRequest(task_id="busy", skill_name="test", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999))
        res = await node._process_message(req)

        assert res.status == "failed"
        assert res.error["code"] == "PROVIDER_BUSY"

    finally:
        await node.stop()
