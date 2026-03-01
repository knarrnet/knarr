import asyncio
import json
import pytest
import os
from knarr.dashboard.server import CockpitServer
from knarr.core.messages import TaskResult
from unittest.mock import MagicMock


class MockNode:
    def __init__(self):
        self._signing_key = MagicMock()
        self.node_info = MagicMock()
        self.node_info.node_id = "test-node"
        self.node_info.host = "127.0.0.1"
        self.node_info.port = 9000
        self._asset_dir = "/tmp/knarr-test-assets"
        self._handlers = {}
        self.storage = MagicMock()
        self.storage.insert_remote_job = MagicMock(return_value=True)
        os.makedirs(self._asset_dir, exist_ok=True)

    def get_status(self):
        return {"node_id": "test-node", "version": "0.8.3", "uptime_seconds": 10,
                "peer_count": 0, "skill_count": 0, "task_slots": {"used": 0, "total": 4},
                "advertise_host": "127.0.0.1", "port": 9000}

    def get_peers(self): return []

    def get_skills(self):
        return {"local": [], "network": [
            {"name": "echo", "version": "1.0", "description": "Echo",
             "providers": [{"node_id": "p1", "host": "127.0.0.1", "port": 9001, "sidecar_port": 0}]}
        ]}

    def get_tasks(self): return []
    def get_ledger(self): return []

    def get_skill_schema(self, name):
        if name == "echo":
            return {"name": "echo", "version": "1.0", "description": "Echo service",
                    "input_schema": {"text": "string"}, "output_schema": {"text": "string"},
                    "providers": []}
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


EXPOSURES = {
    "test-echo": {
        "skill": "echo",
        "path": "echo",
        "enabled": True,
        "rate_limit": 3,
        "presets": {"mode": "preset-value"},
        "fields": {
            "text": {"label": "Your message", "required": True},
        },
        "display": {
            "title": "Echo Test",
            "description": "Send a message and get it back",
            "result_format": "text",
        },
        "provider": {"strategy": "first"},
    },
    "disabled": {
        "skill": "echo",
        "path": "disabled",
        "enabled": False,
    },
}


@pytest.mark.asyncio
async def test_exposure_form_page():
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0, exposures=EXPOSURES)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /s/echo HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"200 OK" in response
        assert b"text/html" in response
        assert b"Echo Test" in response
        assert b"Your message" in response
        assert b"skillForm" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exposure_execute():
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0, exposures=EXPOSURES)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = json.dumps({"text": "hello"}).encode()
        req = f"POST /s/echo/execute HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        writer.write(req)
        await writer.drain()
        response = await reader.read()
        assert b"202 Accepted" in response
        body_part = response.split(b"\r\n\r\n")[1]
        data = json.loads(body_part)
        assert data["status"] == "accepted"
        assert "job_id" in data
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exposure_execute_preset_override():
    """Sentinel: presets cannot be overridden by user input."""
    node = MockNode()

    # Track what input_data was sent to request_task
    captured = {}
    original_request_task = node.request_task

    async def spy_request_task(node_id, host, port, skill, input_data, timeout_ms):
        captured.update(input_data)
        return await original_request_task(node_id, host, port, skill, input_data, timeout_ms)
    node.request_task = spy_request_task

    server = CockpitServer(node, bind="127.0.0.1", port=0, exposures=EXPOSURES)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # Try to override preset "mode" field — should be rejected as unknown field
        body = json.dumps({"text": "hello", "mode": "hacked"}).encode()
        req = f"POST /s/echo/execute HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        writer.write(req)
        await writer.drain()
        response = await reader.read()
        # "mode" is not in exposed fields, so it should be rejected
        assert b"400" in response
        assert b"Unknown fields" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exposure_schema():
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0, exposures=EXPOSURES)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /s/echo/schema HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"200 OK" in response
        body_part = response.split(b"\r\n\r\n")[1]
        data = json.loads(body_part)
        assert data["title"] == "Echo Test"
        assert "text" in data["fields"]
        assert data["fields"]["text"]["required"] is True
        # Presets should NOT appear in schema
        assert "mode" not in data["fields"]
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exposure_rate_limit():
    """Rate limit enforced: 4th request in 60s returns 429 (limit=3)."""
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0, exposures=EXPOSURES)
    await server.start()
    port = server.port
    try:
        responses = []
        for i in range(4):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"text": f"msg{i}"}).encode()
            req = f"POST /s/echo/execute HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
            writer.write(req)
            await writer.drain()
            response = await reader.read()
            responses.append(response)
            writer.close()
            await writer.wait_closed()
        # First 3 should succeed (202 Accepted for async execution)
        for r in responses[:3]:
            assert b"202 Accepted" in r
        # 4th should be rate-limited
        assert b"429" in responses[3]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exposure_disabled():
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0, exposures=EXPOSURES)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /s/disabled HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"404" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exposure_unknown_path():
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0, exposures=EXPOSURES)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /s/nonexistent HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"404" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exposure_header_injection():
    """Sentinel: SA-8C-001 — field names with CRLF cannot split HTTP response."""
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0, exposures=EXPOSURES)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # Send a field name containing CRLF — should NOT create a new header
        body = json.dumps({"text": "ok", "evil\r\nX-Injected: true": "val"}).encode()
        req = f"POST /s/echo/execute HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        writer.write(req)
        await writer.drain()
        response = await reader.read()
        assert b"400" in response
        # The CRLF must be stripped — status line should NOT end before Content-Type
        # i.e. no line between status and Content-Type that looks like an injected header
        header_section = response.split(b"\r\n\r\n")[0]
        header_lines = header_section.split(b"\r\n")
        # First line is status, remaining are real headers — none should start with X-Injected
        for line in header_lines[1:]:
            assert not line.startswith(b"X-Injected")
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exposure_xss_in_result():
    """Sentinel: SA-8C-002 — form page includes esc() sanitizer for result rendering."""
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0, exposures=EXPOSURES)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /s/echo HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"200 OK" in response
        # The page must contain the esc() sanitizer function
        assert b"function esc(" in response
        # Asset link rendering must use hash validation regex
        assert b"/^[0-9a-f]{64}$/" in response
        # Output values must go through esc()
        assert b"esc(k)" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_content_length_invalid():
    """Sentinel: SA-8C-005 — non-numeric Content-Length returns 400."""
    node = MockNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0, exposures=EXPOSURES)
    await server.start()
    port = server.port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /api/status HTTP/1.1\r\nContent-Length: abc\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"400" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
