import pytest
import asyncio
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_node_port_zero_resolution():
    """Verify that a node created with port 0 resolves to a real port after start()."""
    node = DHTNode("127.0.0.1", 0)
    assert node.node_info.port == 0
    
    await node.start()
    try:
        assert node.node_info.port > 0
        # Also verify NodeInfo host is set (should be 127.0.0.1 by default)
        assert node.node_info.host == "127.0.0.1"
    finally:
        await node.stop()
