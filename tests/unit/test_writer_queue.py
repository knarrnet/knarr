import asyncio
import pytest
from knarr.dht.node import DHTNode
from knarr.core.models import Policy

@pytest.mark.asyncio
async def test_writer_queue_serializes_writes():
    # Start a node with in-memory storage
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    try:
        # Enqueue 10 writes
        async def do_write(i):
            await node._enqueue_write(node.storage.record_demand, "name", f"skill-{i}")
            
        await asyncio.gather(*[do_write(i) for i in range(10)])
        
        # Verify all demands recorded
        demand = node.storage.get_demand()
        assert len(demand) == 10
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_writer_queue_propagates_exceptions():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    try:
        def raise_error(*args):
            raise ValueError("Test error")
            
        with pytest.raises(ValueError, match="Test error"):
            await node._enqueue_write(raise_error)
            
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_writer_queue_handles_concurrent_enqueues():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    try:
        # Enqueue from multiple coroutines
        async def worker(prefix, count):
            for i in range(count):
                await node._enqueue_write(node.storage.record_demand, "tag", f"{prefix}-{i}")
                
        await asyncio.gather(
            worker("a", 5),
            worker("b", 5),
            worker("c", 5)
        )
        
        demand = node.storage.get_demand()
        assert len(demand) == 15
        
    finally:
        await node.stop()
