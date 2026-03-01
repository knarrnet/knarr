import pytest
import asyncio
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_bootstrap_join():
    node_a = DHTNode("127.0.0.1", 9010)
    node_b = DHTNode("127.0.0.1", 9011)
    node_c = DHTNode("127.0.0.1", 9012)
    
    await node_a.start()
    await node_b.start()
    await node_c.start()
    
    try:
        # B joins A
        await node_b.join(["127.0.0.1:9010"])
        # C joins B
        await node_c.join(["127.0.0.1:9011"])
        
        # C should know about A through B
        peers_c = node_c.storage.get_peers()
        peer_ids = [p.node_id for p in peers_c]
        assert node_a.node_info.node_id in peer_ids
        assert node_b.node_info.node_id in peer_ids
        
    finally:
        await node_a.stop()
        await node_b.stop()
        await node_c.stop()
