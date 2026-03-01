import asyncio
import time
import pytest
from knarr.dht.node import DHTNode
from knarr.core.messages import Heartbeat, TaskRequest, verify_message, sign_message

@pytest.mark.asyncio
async def test_blocking_handler_does_not_stall_event_loop():
    # Node with a blocking handler
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    # Register blocking handler
    def blocking_handler(data):
        time.sleep(2)
        return {"result": "ok"}
    
    node.register_handler("blocking", blocking_handler)
    await node.announce({
        "name": "blocking", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}
    })
    
    try:
        # Submit task
        task_req = node._sign(TaskRequest(
            task_id="t1",
            requester_node_id="req1",
            requester_host="127.0.0.1",
            requester_port=9999,
            skill_name="blocking",
            input_data={}
        ))
        
        # We simulate incoming connection since we want to check node responsiveness
        # while handler is running.
        
        # Start task in background
        task_fut = asyncio.create_task(node._process_message(task_req))
        
        # Give it a moment to start
        await asyncio.sleep(0.1)
        
        # While task is blocked in time.sleep(2), node should respond to heartbeat
        hb_req = node._sign(Heartbeat(node_id="peer1", timestamp=time.time()))
        
        start_wait = time.time()
        hb_resp = await asyncio.wait_for(node._process_message(hb_req), timeout=1.0)
        end_wait = time.time()
        
        assert isinstance(hb_resp, Heartbeat)
        assert end_wait - start_wait < 0.5 # Should be very fast
        
        # Wait for task completion
        await task_fut
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_blocking_async_handler_does_not_stall_event_loop():
    """Async handler with blocking sync work (regex, HTTP) runs in thread pool,
    so the main event loop stays responsive for heartbeats."""
    node = DHTNode("127.0.0.1", 0)
    await node.start()

    async def blocking_async_handler(data):
        # Simulate blocking sync work inside an async handler (the NZZ bug)
        time.sleep(2)
        return {"result": "ok"}

    node.register_handler("blocking-async", blocking_async_handler)
    await node.announce({
        "name": "blocking-async", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}
    })

    try:
        task_req = node._sign(TaskRequest(
            task_id="t_ba",
            requester_node_id="req1",
            requester_host="127.0.0.1",
            requester_port=9999,
            skill_name="blocking-async",
            input_data={}
        ))

        # Start blocking async task in background
        task_fut = asyncio.create_task(node._process_message(task_req))
        await asyncio.sleep(0.2)

        # While handler is blocked in time.sleep(2), event loop should stay responsive
        hb_req = node._sign(Heartbeat(node_id="peer1", timestamp=time.time()))
        start_wait = time.time()
        hb_resp = await asyncio.wait_for(node._process_message(hb_req), timeout=1.0)
        elapsed = time.time() - start_wait

        assert isinstance(hb_resp, Heartbeat)
        assert elapsed < 0.5, f"Event loop blocked for {elapsed:.1f}s — async handler not isolated"

        await task_fut
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_async_handler_in_thread_pool():
    node = DHTNode("127.0.0.1", 0)
    await node.start()

    async def async_handler(data):
        await asyncio.sleep(0.1)
        return {"val": data["x"] * 2}

    node.register_handler("async-skill", async_handler)
    await node.announce({
        "name": "async-skill", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {"x": "int"}, "output_schema": {"val": "int"}
    })

    try:
        req = node._sign(TaskRequest(
            task_id="t2", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999,
            skill_name="async-skill", input_data={"x": 10}
        ))

        resp = await node._process_message(req)
        assert resp.status == "completed"
        assert resp.output_data["val"] == 20

    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_sync_handler_in_thread_pool():
    node = DHTNode("127.0.0.1", 0)
    await node.start()

    def sync_handler(data):
        return {"val": data["x"] + 5}

    node.register_handler("sync-skill", sync_handler)
    await node.announce({
        "name": "sync-skill", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {"x": "int"}, "output_schema": {"val": "int"}
    })

    try:
        req = node._sign(TaskRequest(
            task_id="t3", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999,
            skill_name="sync-skill", input_data={"x": 10}
        ))

        resp = await node._process_message(req)
        assert resp.status == "completed"
        assert resp.output_data["val"] == 15

    finally:
        await node.stop()
