import asyncio
import json
import pytest
import os
from knarr.dashboard.server import CockpitServer
from knarr.core.messages import TaskResult
from unittest.mock import MagicMock, AsyncMock


class MockNode:
    def __init__(self):
        self._signing_key = MagicMock()
        self.node_info = MagicMock()
        self.node_info.node_id = "test-node"
        self.node_info.host = "127.0.0.1"
        self.node_info.port = 9000
        self._asset_dir = "/tmp/knarr-test-assets"
        self._handlers = {}  # No local handlers by default
        self._sidecar_port = 0
        os.makedirs(self._asset_dir, exist_ok=True)
        # Mock storage for exposure skill validation (C5)
        self.storage = MagicMock()
        self.storage.query_all_active_skills = MagicMock(return_value=[
            {"skill_sheet": {"name": "echo"}, "node_id": "p1", "host": "127.0.0.1", "port": 9001}
        ])

    def get_status(self):
        return {"node_id": "test-node", "version": "0.8.1", "uptime_seconds": 10,
                "peer_count": 0, "skill_count": 0, "task_slots": {"used": 0, "total": 4},
                "advertise_host": "127.0.0.1", "port": 9000}

    def get_peers(self): return []

    def get_skills(self):
        return {"local": [], "network": [
            {"name": "echo", "version": "1.0", "description": "Echo",
             "providers": [{"node_id": "p1", "host": "127.0.0.1", "port": 9001, "sidecar_port": 9002}]}
        ]}

    def get_tasks(self): return []
    def get_ledger(self): return []
    _sidecar = None  # No sidecar in mock by default

    def get_secrets_summary(self):
        return {"echo": {"api_key": {"filled": True, "masked": "***(12 chars)"}}}

    def set_secret(self, skill, key, value):
        pass

    def delete_secret(self, skill, key):
        pass

    def get_economy_summary(self):
        return {"peers": [{"node_id": "abc123", "public_key": "abc123full", "group": "",
                           "balance": -5.0, "credit_limit": 13.0, "utilization_pct": 61.5,
                           "status": "amber", "tasks_provided": 3, "tasks_consumed": 8,
                           "last_activity": 1700000000}],
                "summary": {"total_red": -5.0, "total_black": 0, "net_position": -5.0,
                             "peers_green": 0, "peers_amber": 1, "peers_red": 0}}

    def get_skill_schema(self, name):
        if name == "echo":
            return {"name": "echo", "version": "1.0", "description": "Echo",
                    "input_schema": {"text": "string"}, "output_schema": {"text": "string"},
                    "providers": [{"node_id": "p1", "host": "127.0.0.1", "port": 9001, "sidecar_port": 9002, "load": 2}]}
        return None

    def asset_path(self, h):
        return os.path.join(self._asset_dir, h)

    async def request_task(self, node_id, host, port, skill, input_data, timeout_ms):
        return TaskResult(task_id="t1", status="completed", output_data={"text": input_data.get("text", "")})

    async def submit_async_task(self, node_id, host, port, skill, input_data, timeout_ms=30000):
        status = MagicMock()
        status.task_id = "job-test-1"
        status.status = "accepted"
        status.position = 0
        return status


@pytest.mark.asyncio
async def test_execute_auth_required():
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0, auth_token="secret")
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        req = b"POST /api/execute HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}"
        writer.write(req)
        await writer.drain()
        response = await reader.read()
        assert b"401 Unauthorized" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_execute_returns_result():
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = json.dumps({"skill": "echo", "input": {"text": "hello"}}).encode()
        req = f"POST /api/execute HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        writer.write(req)
        await writer.drain()
        response = await reader.read()
        assert b"202 Accepted" in response
        body_part = response.split(b"\r\n\r\n")[1]
        data = json.loads(body_part)
        assert "job_id" in data
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_schema_endpoint():
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /api/skills/echo/schema HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"200 OK" in response
        body_part = response.split(b"\r\n\r\n")[1]
        data = json.loads(body_part)
        assert data["name"] == "echo"
        assert "input_schema" in data
        assert "providers" in data
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_schema_404():
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /api/skills/nonexistent/schema HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"404 Not Found" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_asset_proxy_local():
    node = MockNode()
    h = "a" * 64
    path = node.asset_path(h)
    with open(path, "wb") as f:
        f.write(b"\x00\x01\x02\xff\xfe\xfd")
    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET /api/assets/{h} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        response = await reader.read()
        assert b"200 OK" in response
        assert b"\x00\x01\x02\xff\xfe\xfd" in response
        assert b"application/octet-stream" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
        if os.path.exists(path):
            os.remove(path)


@pytest.mark.asyncio
async def test_execute_body_size_limit():
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = b"{" + b"a" * 70000 + b"}"
        req = f"POST /api/execute HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        writer.write(req)
        await writer.drain()
        response = await reader.read()
        assert b"413 Request Too Large" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_asset_hash_validation():
    """Sentinel: path traversal via invalid hash is rejected."""
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /api/assets/../../etc/passwd HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"400" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_execute_local_skill():
    """Local skills execute via call_local, bypassing network."""
    node = MockNode()
    node._handlers = {"echo": (lambda d: {"text": d.get("text", "")}, False)}

    async def mock_call_local(skill, input_data):
        handler_fn, _ = node._handlers[skill.lower()]
        return handler_fn(input_data)
    node.call_local = mock_call_local

    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = json.dumps({"skill": "echo", "input": {"text": "local"}, "local": True}).encode()
        req = f"POST /api/execute HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        writer.write(req)
        await writer.drain()
        response = await reader.read()
        assert b"202 Accepted" in response
        body_part = response.split(b"\r\n\r\n")[1]
        data = json.loads(body_part)
        assert "job_id" in data
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_economy_endpoint():
    """G-6: Economy endpoint returns aggregated peer data."""
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /api/economy HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"200 OK" in response
        body_part = response.split(b"\r\n\r\n")[1]
        data = json.loads(body_part)
        assert "peers" in data
        assert "summary" in data
        assert data["summary"]["peers_amber"] == 1
        assert data["peers"][0]["balance"] == -5.0
        assert data["peers"][0]["utilization_pct"] == 61.5
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_secrets_endpoint():
    """G-9: Secrets endpoint returns masked secret status."""
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /api/secrets HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"200 OK" in response
        body_part = response.split(b"\r\n\r\n")[1]
        data = json.loads(body_part)
        assert "echo" in data
        assert data["echo"]["api_key"]["filled"] is True
        assert "***" in data["echo"]["api_key"]["masked"]
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_secret_set_and_delete():
    """G-9: PUT and DELETE on /api/secrets/{skill}/{key}."""
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port
    try:
        # PUT
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = json.dumps({"value": "test-secret"}).encode()
        req = f"PUT /api/secrets/echo/api_key HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        writer.write(req)
        await writer.drain()
        response = await reader.read()
        assert b"200 OK" in response
        writer.close()
        await writer.wait_closed()

        # DELETE
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"DELETE /api/secrets/echo/api_key HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"200 OK" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exposure_crud():
    """G-5: Create, list, and delete exposures via API."""
    import tempfile
    node = MockNode()
    with tempfile.TemporaryDirectory() as tmpdir:
        server = CockpitServer(node, bind="127.0.0.1", port=0, config_dir=tmpdir)
        await server.start()
        port = server.port
        try:
            # Create
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"name": "test-exp", "skill": "echo", "path": "test-exp"}).encode()
            req = f"POST /api/exposures HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
            writer.write(req)
            await writer.drain()
            response = await reader.read()
            assert b"200 OK" in response
            writer.close()
            await writer.wait_closed()

            # List
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /api/exposures HTTP/1.1\r\n\r\n")
            await writer.drain()
            response = await reader.read()
            assert b"200 OK" in response
            data = json.loads(response.split(b"\r\n\r\n")[1])
            assert len(data["exposures"]) == 1
            assert data["exposures"][0]["name"] == "test-exp"
            assert data["exposures"][0]["skill"] == "echo"
            writer.close()
            await writer.wait_closed()

            # Verify expose.toml written
            assert os.path.exists(os.path.join(tmpdir, "expose.toml"))

            # Delete
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"DELETE /api/exposures/test-exp HTTP/1.1\r\n\r\n")
            await writer.drain()
            response = await reader.read()
            assert b"200 OK" in response
            writer.close()
            await writer.wait_closed()

            # Verify empty
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /api/exposures HTTP/1.1\r\n\r\n")
            await writer.drain()
            response = await reader.read()
            data = json.loads(response.split(b"\r\n\r\n")[1])
            assert len(data["exposures"]) == 0
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()


@pytest.mark.asyncio
async def test_asset_delete_requires_auth():
    """Sentinel CG-001: DELETE /api/assets/{hash} must require auth when token configured."""
    node = MockNode()
    h = "b" * 64
    path = node.asset_path(h)
    with open(path, "wb") as f:
        f.write(b"test")
    server = CockpitServer(node, bind="127.0.0.1", port=0, auth_token="secret")
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"DELETE /api/assets/{h} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        response = await reader.read()
        assert b"401 Unauthorized" in response
        # File must still exist
        assert os.path.exists(path)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
        if os.path.exists(path):
            os.remove(path)


@pytest.mark.asyncio
async def test_asset_proxy_requires_auth():
    """Sentinel CG-002: GET /api/assets with remote proxy params must require auth."""
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0, auth_token="secret")
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        h = "c" * 64
        writer.write(f"GET /api/assets/{h}?host=127.0.0.1&sidecar_port=9999 HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        response = await reader.read()
        assert b"401 Unauthorized" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
