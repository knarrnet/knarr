import asyncio
import pytest
import time
from dataclasses import replace
from knarr.dht.node import DHTNode
from knarr.core.messages import TaskRequest

@pytest.mark.asyncio
async def test_task_queue_accepts_within_capacity():
    # Provider with task_slots=2
    node = DHTNode("127.0.0.1", 0, config={"node": {"task_slots": 2}})
    await node.start()
    
    async def handler(data):
        await asyncio.sleep(0.1)
        return {"res": "ok"}
        
    node.register_handler("test", handler)
    await node.announce({
        "name": "test", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}
    })
    
    try:
        # Submit 2 tasks
        req1 = node._sign(TaskRequest(task_id="t1", skill_name="test", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999))
        req2 = node._sign(TaskRequest(task_id="t2", skill_name="test", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999))
        
        res1, res2 = await asyncio.gather(
            node._process_message(req1),
            node._process_message(req2)
        )
        
        assert res1.status == "completed"
        assert res2.status == "completed"
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_task_queue_rejects_when_full():
    """Slow tasks fill the queue; next slow task gets PROVIDER_BUSY."""
    # task_slots=1, max_queue_depth=2: queue holds 2 items before rejecting
    # Use external public_key to bypass the v0.37.0 A1 self-call fast path.
    # _handle_task_request does not verify signatures, so replacing public_key is safe.
    node = DHTNode("127.0.0.1", 0, config={"node": {"task_slots": 1, "max_queue_depth": 2}})
    await node.start()

    async def slow_handler(data):
        await asyncio.sleep(0.5)
        return {"res": "ok"}

    node.register_handler("slow", slow_handler, slow=True)
    await node.announce({
        "name": "slow", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}
    })

    ext_key = "bb" * 32  # 64-char hex external caller — bypasses is_self_call fast path

    try:
        req1 = replace(node._sign(TaskRequest(task_id="t1", skill_name="slow", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999)), public_key=ext_key)
        req2 = replace(node._sign(TaskRequest(task_id="t2", skill_name="slow", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999)), public_key=ext_key)
        req3 = replace(node._sign(TaskRequest(task_id="t3", skill_name="slow", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999)), public_key=ext_key)
        req4 = replace(node._sign(TaskRequest(task_id="t4", skill_name="slow", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999)), public_key=ext_key)

        # t1: worker available, enqueued + accepted (slow)
        t1 = asyncio.create_task(node._process_message(req1))
        await asyncio.sleep(0.1)
        # t2: workers saturated, enqueued + queued status (slow)
        t2 = asyncio.create_task(node._process_message(req2))
        await asyncio.sleep(0.05)
        # t3: workers saturated, enqueued + queued status (fills queue to maxsize=2)
        t3 = asyncio.create_task(node._process_message(req3))
        await asyncio.sleep(0.05)

        # t4: workers saturated, queue full → PROVIDER_BUSY
        res4 = await node._process_message(req4)
        assert res4.status == "failed"
        assert res4.error["code"] == "PROVIDER_BUSY"

        await t1
        # t2 and t3 returned TaskStatus immediately, just ensure they resolved
        await t2
        await t3

    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_task_queue_drains_in_order():
    """Slow tasks enqueued while workers busy execute in FIFO order."""
    node = DHTNode("127.0.0.1", 0, config={"node": {"task_slots": 1}})
    await node.start()

    results = []
    async def handler(data):
        results.append(data["id"])
        await asyncio.sleep(0.1)
        return {}

    node.register_handler("order", handler, slow=True)
    await node.announce({
        "name": "order", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {"id": "int"}, "output_schema": {}
    })

    try:
        req1 = node._sign(TaskRequest(task_id="t1", skill_name="order", input_data={"id": 1}, requester_node_id="r", requester_host="127.0.0.1", requester_port=9999))
        req2 = node._sign(TaskRequest(task_id="t2", skill_name="order", input_data={"id": 2}, requester_node_id="r", requester_host="127.0.0.1", requester_port=9999))

        # Start both — first executes, second enqueues (slow tasks enqueue when workers busy)
        asyncio.create_task(node._process_message(req1))
        await asyncio.sleep(0.05)
        asyncio.create_task(node._process_message(req2))

        await asyncio.sleep(0.4)
        assert results == [1, 2]

    finally:
        await node.stop()

def test_task_slots_configurable():
    node = DHTNode("127.0.0.1", 0, config={"node": {"task_slots": 8}})
    assert node._task_slots == 8

def test_task_slots_default():
    node = DHTNode("127.0.0.1", 0)
    assert node._task_slots == 4
