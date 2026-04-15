import asyncio
import struct
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar
from ..core.messages import Message, serialize_message, deserialize_message, verify_message, verify_node_id
from ..core.crypto import get_tls_peer_cert_fingerprint

class ProtocolError(Exception):
    """Raised for protocol-level errors."""
    pass

MAX_MESSAGE_SIZE = 1_048_576  # 1MB
T = TypeVar("T", bound=Message)


@dataclass(frozen=True, slots=True)
class TransportMetadata:
    peer_cert_fingerprint: str = ""
    peer_host: str = ""
    peer_port: int = 0

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
                           ssl_context=None) -> tuple[Optional[Message], TransportMetadata]:
    """Connects, sends a message, and waits for a single response."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_context),
            timeout=timeout
        )
        metadata = TransportMetadata(
            peer_cert_fingerprint=get_tls_peer_cert_fingerprint(writer.get_extra_info("ssl_object")),
            peer_host=host,
            peer_port=int(port),
        )
        try:
            await send_message(writer, msg)
            response = await asyncio.wait_for(
                receive_message(reader),
                timeout=timeout
            )
            return response, metadata
        finally:
            writer.close()
            await writer.wait_closed()
    except (asyncio.TimeoutError, ConnectionError, Exception):
        return None, TransportMetadata()


async def request_response_expecting(
    host: str,
    port: int,
    req: Message,
    response_type: type[T],
    *,
    timeout: float = 5.0,
    ssl_context=None,
    require_node_id: bool = False,
    verify_callback: Optional[Callable[[Message, TransportMetadata], bool]] = None,
    _send_fn=None,
) -> Optional[T]:
    if _send_fn is not None:
        result = await _send_fn(host, port, req, timeout=timeout, ssl_context=ssl_context)
        if result is None:
            response, transport = None, TransportMetadata()
        else:
            response, transport = result
            if transport is None:
                transport = TransportMetadata()
    else:
        response, transport = await request_response(host, port, req, timeout=timeout, ssl_context=ssl_context)
    if response is None or not isinstance(response, response_type):
        return None
    if not verify_message(response):
        return None
    if require_node_id and not verify_node_id(response):
        return None
    if verify_callback is not None:
        if not verify_callback(response, transport):
            return None
    return response
