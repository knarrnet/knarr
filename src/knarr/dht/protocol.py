import asyncio
import struct
from typing import Optional
from ..core.messages import Message, serialize_message, deserialize_message
from ..core.crypto import get_tls_peer_cert_fingerprint

class ProtocolError(Exception):
    """Raised for protocol-level errors."""
    pass

MAX_MESSAGE_SIZE = 1_048_576  # 1MB

async def send_message(writer: asyncio.StreamWriter, msg: Message):
    """Sends a length-prefixed JSON message."""
    body = serialize_message(msg)
    header = struct.pack(">I", len(body))
    writer.write(header + body)
    await writer.drain()

async def receive_message(reader: asyncio.StreamReader, max_size: int = MAX_MESSAGE_SIZE) -> Optional[Message]:
    """Receives a length-prefixed JSON message."""
    try:
        header = await reader.readexactly(4)
        length = struct.unpack(">I", header)[0]
        
        if length > max_size:
            raise ProtocolError(f"Message too large: {length} bytes (max {max_size})")
            
        body = await reader.readexactly(length)
        return deserialize_message(body)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None

async def request_response(host: str, port: int, msg: Message, timeout: float = 5.0,
                           ssl_context=None) -> Optional[Message]:
    """Connects, sends a message, and waits for a single response."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_context),
            timeout=timeout
        )
        fingerprint = get_tls_peer_cert_fingerprint(writer.get_extra_info("ssl_object"))
        try:
            await send_message(writer, msg)
            response = await asyncio.wait_for(
                receive_message(reader),
                timeout=timeout
            )
            if response is not None:
                object.__setattr__(response, "_tls_peer_cert_fingerprint", fingerprint)
                object.__setattr__(response, "_tls_peer_host", host)
                object.__setattr__(response, "_tls_peer_port", int(port))
            return response
        finally:
            writer.close()
            await writer.wait_closed()
    except (asyncio.TimeoutError, ConnectionError, Exception):
        return None
