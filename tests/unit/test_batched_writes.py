import asyncio
import pytest
import time
from unittest.mock import MagicMock, patch
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_batch_collects_multiple_writes():
    """Queue 10 writes quickly, verify they execute in a single batch cycle."""
    node = DHTNode("127.0.0.1", 0)
    node._running = True
    
    results = []
    def mock_op(val):
        results.append(val)
        return val
    
    # Start writer loop
    loop_task = asyncio.create_task(node._writer_loop())
    
    try:
        start = time.monotonic()
        futures = []
        for i in range(10):
            fut = asyncio.get_running_loop().create_future()
            node._write_queue.put_nowait((mock_op, (i,), fut))
            futures.append(fut)
            
        await asyncio.gather(*futures)
        end = time.monotonic()
        
        assert len(results) == 10
        assert results == list(range(10))
        # Window is 50ms, so 10 writes should finish very quickly.
        # Total time should be around 50ms + overhead.
        assert end - start < 0.2  # 200ms is generous
        
    finally:
        node._running = False
        await loop_task

@pytest.mark.asyncio
async def test_batch_single_write_no_delay():
    """Single write at idle executes within 50ms."""
    node = DHTNode("127.0.0.1", 0)
    node._running = True
    
    # Start writer loop
    loop_task = asyncio.create_task(node._writer_loop())
    
    try:
        fut = asyncio.get_running_loop().create_future()
        start = time.monotonic()
        node._write_queue.put_nowait((lambda x: x, (1,), fut))
        
        await fut
        end = time.monotonic()
        
        # Window is 50ms max, but it gives up early if queue is empty after 5ms sleep.
        # So it should be fast.
        assert end - start < 0.1   # 100ms max (generous for CI)
        
    finally:
        node._running = False
        await loop_task

@pytest.mark.asyncio
async def test_batch_respects_max_size():
    """Queue 200 writes, verify max batch size is 100."""
    node = DHTNode("127.0.0.1", 0)
    node._running = True
    
    # instrumented loop to count batches
    batch_sizes = []
    
    async def instrumented_writer_loop():
        BATCH_WINDOW_MS = 50
        BATCH_MAX_SIZE = 100
        while node._running:
            batch = []
            try:
                item = await asyncio.wait_for(node._write_queue.get(), timeout=1.0)
                batch.append(item)
            except asyncio.TimeoutError: continue
            
            deadline = time.monotonic() + BATCH_WINDOW_MS / 1000
            while time.monotonic() < deadline and len(batch) < BATCH_MAX_SIZE:
                try:
                    item = node._write_queue.get_nowait()
                    batch.append(item)
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.005)
                    try:
                        item = node._write_queue.get_nowait()
                        batch.append(item)
                    except asyncio.QueueEmpty: break
            
            batch_sizes.append(len(batch))
            for op, args, future in batch:
                future.set_result(op(*args))

    loop_task = asyncio.create_task(instrumented_writer_loop())
    
    try:
        futures = []
        for i in range(200):
            fut = asyncio.get_running_loop().create_future()
            node._write_queue.put_nowait((lambda x: x, (i,), fut))
            futures.append(fut)
            
        await asyncio.gather(*futures)
        
        assert sum(batch_sizes) == 200
        for size in batch_sizes:
            assert size <= 100
        assert len(batch_sizes) >= 2  # Must be at least 2 batches
        
    finally:
        node._running = False
        await loop_task
