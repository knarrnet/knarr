import pytest
import asyncio
import time
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_provider_busy_consumer_tries_next():
    # 2 providers for same skill
    p1 = DHTNode("127.0.0.1", 9800, config={"node": {"task_slots": 1}})
    p2 = DHTNode("127.0.0.1", 9801, config={"node": {"task_slots": 1}})
    
    await p1.start()
    await p2.start()
    
    async def slow_handler(data):
        await asyncio.sleep(5.0)
        return {"provider": data["p"]}
        
    p1.register_handler("test", slow_handler)
    p2.register_handler("test", slow_handler)
    
    await p1.announce({"name": "test", "version": "1.0.0", "description": "d", "tags": ["t"], "input_schema": {}, "output_schema": {}})
    await p2.announce({"name": "test", "version": "1.0.0", "description": "d", "tags": ["t"], "input_schema": {}, "output_schema": {}})
    
    consumer = DHTNode("127.0.0.1", 9802)
    await consumer.start()
    await consumer.join(["127.0.0.1:9800"])
    await consumer.join(["127.0.0.1:9801"])
    await asyncio.sleep(0.5) 
    
    try:
        from knarr.core.messages import TaskRequest
        async def dummy_handler(data):
            await asyncio.sleep(10)
            return {}
            
        # Saturate p1
        for i in range(2):
            req = TaskRequest(task_id=f"dummy{i}", skill_name="test")
            fut = asyncio.get_running_loop().create_future()
            p1._task_queue.put_nowait((req, dummy_handler, False, 0, time.time(), fut))
        p1._active_workers = 1
        
        # Discover providers
        results = await consumer.query("name", "test")
        assert len(results) == 2
        
        # p1 should report load=10
        # p2 should report load=0
        assert results[0]["node_id"] == p2.node_info.node_id
        
        # Request should go to p2 and succeed
        res = await consumer.request_task(results[0]["node_id"], results[0]["host"], results[0]["port"], "test", {"p": 2})
        assert res.status == "completed"
        assert res.output_data["provider"] == 2
        
    finally:
        await p1.stop()
        await p2.stop()
        await consumer.stop()

@pytest.mark.asyncio
async def test_retry_after_consumer_waits():
    p1 = DHTNode("127.0.0.1", 9810, config={"node": {"task_slots": 1}})
    await p1.start()
    
    async def handler(data):
        return {"res": "ok"}
    p1.register_handler("test", handler)
    await p1.announce({"name": "test", "version": "1.0.0", "description": "d", "tags": ["t"], "input_schema": {}, "output_schema": {}})
    
    consumer = DHTNode("127.0.0.1", 9811)
    await consumer.start()
    
    try:
        from knarr.core.messages import TaskRequest
        async def dummy(d): await asyncio.sleep(100); return {}
        
        # 1 worker busy
        p1._active_workers = 1
        # 2 items in queue (75% of 2 is 1)
        for i in range(2):
            fut = asyncio.get_running_loop().create_future()
            p1._task_queue.put_nowait((TaskRequest(task_id=f"m{i}", skill_name="test"), dummy, False, 0, time.time(), fut))
            
        # qsize should be 1 or 2 depending on if worker dequeued
        # If worker dequeued m0, qsize is 1. 1 >= 1 is True.
        
        # Next request gets RETRY_AFTER
        res = await consumer.request_task(p1.node_info.node_id, "127.0.0.1", 9810, "test", {})
        assert res.status == "failed"
        assert res.error["code"] == "RETRY_AFTER"
        
    finally:
        await p1.stop()
        await consumer.stop()

@pytest.mark.asyncio
async def test_queue_position_displayed():
    # task_slots=4, queue_max=8. 75% = 6.
    p1 = DHTNode("127.0.0.1", 9820, config={"node": {"task_slots": 4}})
    await p1.start()
    
    async def slow_handler(data):
        await asyncio.sleep(5.0)
        return {"res": "ok"}
    p1.register_handler("slow", slow_handler, slow=True)
    await p1.announce({"name": "slow", "version": "1.0.0", "description": "d", "tags": ["t"], "input_schema": {}, "output_schema": {}})
    
    consumer = DHTNode("127.0.0.1", 9821)
    await consumer.start()
    
    try:
        # 1. Occupy all workers
        for i in range(4):
            asyncio.create_task(consumer.request_task(p1.node_info.node_id, "127.0.0.1", 9820, "slow", {}, timeout_ms=60000))
        await asyncio.sleep(0.5)
        assert p1._active_workers == 4
        
        # 2. Put 2 items in queue manually
        from knarr.core.messages import TaskRequest, TaskStatus
        for i in range(2):
            fut = asyncio.get_running_loop().create_future()
            p1._task_queue.put_nowait((TaskRequest(task_id=f"m{i}", skill_name="slow"), slow_handler, True, 0, time.time(), fut))
        
        # 3. Send next task via _process_message
        # Current depth is 2. New one makes it 3.
        # 3 < 6 (RETRY_AFTER threshold), so it should be "queued".
        req = consumer._sign(TaskRequest(
            task_id="t_final", requester_node_id=consumer.node_info.node_id,
            requester_host=consumer.node_info.host, requester_port=consumer.node_info.port,
            skill_name="slow", input_data={}
        ))
        
        resp = await p1._process_message(req)
        assert isinstance(resp, TaskStatus)
        assert resp.status == "queued"
        assert resp.position == 3
        
    finally:
        await p1.stop()
        await consumer.stop()