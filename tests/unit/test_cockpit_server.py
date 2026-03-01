import asyncio
import json
import pytest
from knarr.dashboard.server import CockpitServer
from unittest.mock import MagicMock

class MockNode:
    def get_status(self): return {"status": "ok"}
    def get_peers(self): return []
    def get_skills(self): return {"local": [], "network": []}
    def get_tasks(self): return []
    def get_ledger(self): return []

@pytest.mark.asyncio
async def test_cockpit_serves_api_status():
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port
    
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        req = """GET /api/status HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"""
        writer.write(req.encode())
        await writer.drain()
        
        response = await reader.read()
        assert b"200 OK" in response
        assert b"application/json" in response
        
        # Extract body
        body = response.split(b"\r\n\r\n")[1]
        data = json.loads(body)
        assert data == {"status": "ok"}
        
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

@pytest.mark.asyncio
async def test_cockpit_auth_required():
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0, auth_token="secret")
    await server.start()
    port = server.port
    
    try:
        # 1. No auth
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /api/status HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"401 Unauthorized" in response
        writer.close()
        await writer.wait_closed()
        
        # 2. Correct auth
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /api/status HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer secret\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"200 OK" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

@pytest.mark.asyncio
async def test_cockpit_connection_limit():
    """Sentinel: cockpit rejects connections beyond max_connections (8)."""
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port

    try:
        # Open 8 connections that hold by not sending any data (blocking on readline)
        holders = []
        for _ in range(8):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            holders.append((r, w))

        await asyncio.sleep(0.1)  # let server accept and register all 8

        # 9th connection should be rejected (closed or reset)
        r9, w9 = await asyncio.open_connection("127.0.0.1", port)
        rejected = False
        try:
            w9.write(b"GET /api/status HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await w9.drain()
            data = await asyncio.wait_for(r9.read(), timeout=2.0)
            rejected = (data == b"")
        except (ConnectionResetError, BrokenPipeError):
            rejected = True
        assert rejected, "9th connection should be rejected when 8 are held"
        w9.close()

        for r, w in holders:
            w.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_cockpit_404():
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port
    
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /nonexistent HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"404 Not Found" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
