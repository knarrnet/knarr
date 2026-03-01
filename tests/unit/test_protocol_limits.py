import pytest
import asyncio
import struct
import json
from knarr.dht.protocol import receive_message, ProtocolError, MAX_MESSAGE_SIZE

class MockReader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    async def readexactly(self, n: int) -> bytes:
        if self.offset + n > len(self.data):
            raise asyncio.IncompleteReadError(self.data[self.offset:], n)
        res = self.data[self.offset:self.offset+n]
        self.offset += n
        return res

@pytest.mark.asyncio
async def test_protocol_size_limit_accepted():
    # Message at exactly 1MB
    msg_body = b'{"type": "ACK", "status": "ok"}'
    # Fill up to 1MB
    padding = b' ' * (MAX_MESSAGE_SIZE - len(msg_body))
    full_body = msg_body[:-1] + padding + b'}'
    
    header = struct.pack(">I", len(full_body))
    reader = MockReader(header + full_body)
    
    # This should work (no exception)
    msg = await receive_message(reader)
    assert msg is not None
    assert msg.type == "ACK"

@pytest.mark.asyncio
async def test_protocol_size_limit_rejected():
    # Message over 1MB
    body_len = MAX_MESSAGE_SIZE + 1
    header = struct.pack(">I", body_len)
    reader = MockReader(header + b' ' * body_len)
    
    with pytest.raises(ProtocolError, match="Message too large"):
        await receive_message(reader)

@pytest.mark.asyncio
async def test_protocol_normal_message():
    msg_body = json.dumps({"type": "ACK", "status": "ok"}).encode()
    header = struct.pack(">I", len(msg_body))
    reader = MockReader(header + msg_body)
    
    msg = await receive_message(reader)
    assert msg is not None
    assert msg.type == "ACK"
