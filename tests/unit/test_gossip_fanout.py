import asyncio
import pytest
import random
from unittest.mock import AsyncMock, patch, MagicMock
from knarr.dht.node import DHTNode
from knarr.core.messages import Announce, Deregister, TaskRequest, NodeInfo

@pytest.fixture
def mock_node():
    node = DHTNode("127.0.0.1", 0)
    node._gossip_fanout = 3
    node._send_to_peer = AsyncMock()
    node._enqueue_write = AsyncMock() # Prevent hangs on DB writes
    # Mock storage to return 10 peers
    node.storage.get_peers = MagicMock(return_value=[
        NodeInfo(node_id=f"peer{i}", host="1.1.1.1", port=9000+i)
        for i in range(10)
    ])
    return node

@pytest.mark.asyncio
async def test_announce_forwards_to_fanout_peers_only(mock_node):
    """With 10 peers and fanout=3, only 3 receive forwarded announce."""
    from knarr.core.models import SkillSheet
    sheet = SkillSheet(name="test", version="1.0.0", description="test", tags=["test"], input_schema={}, output_schema={})
    
    with patch("knarr.dht.node.validate_skill_sheet", return_value=sheet):
        # Mock _sign to return a dummy message instead of failing on real signing
        mock_node._sign = lambda m: m
        
        await mock_node.announce({"name": "test", "version": "1.0.0", "description": "test", "tags": ["test"], "input_schema": {}, "output_schema": {}})
        
        # announce() forwards to peers.
        assert mock_node._send_to_peer.call_count == 3

@pytest.mark.asyncio
async def test_deregister_forwards_to_fanout_peers_only(mock_node):
    """With 10 peers and fanout=3, only 3 receive forwarded deregister."""
    mock_node._own_skills = {"test": MagicMock()}
    await mock_node.deregister("test")
    assert mock_node._send_to_peer.call_count == 3

@pytest.mark.asyncio
async def test_fanout_caps_at_peer_count():
    """With 2 peers and fanout=3, sends to both without error."""
    node = DHTNode("127.0.0.1", 0)
    node._gossip_fanout = 3
    node._send_to_peer = AsyncMock()
    node._enqueue_write = AsyncMock() # Prevent hang
    node.storage.get_peers = MagicMock(return_value=[
        NodeInfo(node_id="p1", host="1.1.1.1", port=9001),
        NodeInfo(node_id="p2", host="1.1.1.1", port=9002)
    ])
    
    node._own_skills = {"test": MagicMock()}
    await node.deregister("test")
    assert node._send_to_peer.call_count == 2
