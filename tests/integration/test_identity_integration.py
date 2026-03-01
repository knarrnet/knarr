import pytest
import asyncio
from knarr.dht.node import DHTNode
from knarr.core.messages import JoinRequest, sign_message, serialize_message

@pytest.mark.asyncio
async def test_signed_join_accepted():
    node_a = DHTNode("127.0.0.1", 9500)
    node_b = DHTNode("127.0.0.1", 9501)
    
    await node_a.start()
    await node_b.start()
    
    try:
        # B joins A (signs automatically in join())
        joined = await node_b.join(["127.0.0.1:9500"])
        assert joined == True
        
        # Verify node_a knows about node_b
        peers_a = node_a.storage.get_peers()
        assert any(p.node_id == node_b.node_info.node_id for p in peers_a)
        
    finally:
        await node_a.stop()
        await node_b.stop()

@pytest.mark.asyncio
async def test_unsigned_message_rejected():
    node_a = DHTNode("127.0.0.1", 9510)
    await node_a.start()
    
    try:
        # Send raw unsigned bytes to node_a
        reader, writer = await asyncio.open_connection("127.0.0.1", 9510)
        
        # Construct message but don't sign it
        req = JoinRequest(node_id="fake", host="127.0.0.1", port=9511)
        import struct
        body = serialize_message(req)
        header = struct.pack(">I", len(body))
        
        writer.write(header + body)
        await writer.drain()
        
        # Should not get any response or should get None
        from knarr.dht.protocol import receive_message
        resp = await asyncio.wait_for(receive_message(reader), timeout=1.0)
        assert resp is None
        
        writer.close()
        await writer.wait_closed()
        
    finally:
        await node_a.stop()
