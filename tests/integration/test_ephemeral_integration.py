import pytest
import asyncio
from knarr.dht.node import DHTNode
from knarr.core.messages import JoinRequest

@pytest.mark.asyncio
async def test_ephemeral_node_not_added_to_peer_table():
    provider = DHTNode("127.0.0.1", 0)
    await provider.start()
    
    try:
        # One-shot node
        consumer = DHTNode("127.0.0.1", 0, ephemeral=True)
        await consumer.start()
        
        # Join provider
        joined = await consumer.join([f"127.0.0.1:{provider.node_info.port}"])
        assert joined is True
        
        # Verify provider's peer table
        peers = provider.storage.get_peers()
        # Should NOT contain consumer
        assert not any(p.node_id == consumer.node_info.node_id for p in peers)
        
        await consumer.stop()
    finally:
        await provider.stop()

@pytest.mark.asyncio
async def test_non_ephemeral_node_added_to_peer_table():
    provider = DHTNode("127.0.0.1", 0)
    await provider.start()
    
    try:
        # Normal node
        peer = DHTNode("127.0.0.1", 0, ephemeral=False)
        await peer.start()
        
        # Join provider
        joined = await peer.join([f"127.0.0.1:{provider.node_info.port}"])
        assert joined is True
        
        # Verify provider's peer table
        peers = provider.storage.get_peers()
        # SHOULD contain peer
        assert any(p.node_id == peer.node_info.node_id for p in peers)
        
        await peer.stop()
    finally:
        await provider.stop()
