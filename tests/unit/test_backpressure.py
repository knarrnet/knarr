import asyncio
import pytest
import time
from dataclasses import replace
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

    # Use external public_key to bypass v0.37.0 A1 self-call fast path.
    # Saturate _active_workers directly (same pattern as test_provider_busy_when_queue_full)
    # to avoid relying on timing of multiple _enqueue_write calls in _handle_task_request.
    ext_key = "bb" * 32

    try:
        # Simulate one worker occupied (saturates task_slots=1)
        node._active_workers = 1
        assert node._active_workers == 1

        # Fast task (not slow=True): _active_workers >= task_slots AND not slow → RETRY_AFTER
        req2 = replace(node._sign(TaskRequest(task_id="t2", skill_name="test", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999)), public_key=ext_key)
        res2 = await node._process_message(req2)

        assert res2.status == "failed"
        assert res2.error["code"] == "RETRY_AFTER"
        assert res2.error["retry_after_seconds"] >= 1

    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_provider_busy_when_queue_full():
    """Slow task gets PROVIDER_BUSY when queue is full."""
    # task_slots=1, max_queue_depth=2: queue holds 2 items before rejecting
    node = DHTNode("127.0.0.1", 0, config={"node": {"task_slots": 1, "max_queue_depth": 2}})
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
        # External key bypasses self-call fast path so queue admission is reached.
        req = replace(node._sign(TaskRequest(task_id="busy", skill_name="test", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999)), public_key="bb" * 32)
        res = await node._process_message(req)

        assert res.status == "failed"
        assert res.error["code"] == "PROVIDER_BUSY"

    finally:
        await node.stop()
