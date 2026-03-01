"""Tests for POST /api/assets cockpit endpoint."""
import asyncio
import base64
import hashlib
import json
import os
import tempfile

import pytest
from knarr.dashboard.server import CockpitServer


class MockNode:
    def __init__(self, asset_dir=""):
        self._asset_dir = asset_dir
        self._handlers = {"my-skill": (None, False)}
        self._sidecar = None

    def get_status(self): return {"status": "ok"}
    def get_peers(self): return []
    def get_skills(self): return {"local": [], "network": []}
    def get_tasks(self): return []
    def get_ledger(self): return []

    def store_asset(self, data: bytes) -> str:
        content_hash = hashlib.sha256(data).hexdigest()
        path = os.path.join(self._asset_dir, content_hash)
        with open(path, "wb") as f:
            f.write(data)
        return content_hash

    def asset_path(self, h):
        return os.path.join(self._asset_dir, h)


async def _raw_request(port, path, body=None, content_type="application/octet-stream", token=""):
    """Send raw HTTP request and return (status_code, parsed_json_body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    method = "POST" if body is not None else "GET"
    headers = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
    if token:
        headers += f"Authorization: Bearer {token}\r\n"
    if body is not None:
        if isinstance(body, str):
            body = body.encode()
        headers += f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
    headers += "\r\n"
    writer.write(headers.encode())
    if body is not None:
        writer.write(body)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), timeout=5.0)
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    # Parse status code and body
    resp_str = response.decode("utf-8", errors="replace")
    status_line = resp_str.split("\r\n")[0]
    status_code = int(status_line.split(" ")[1])
    body_start = resp_str.find("\r\n\r\n")
    json_body = {}
    if body_start >= 0:
        try:
            json_body = json.loads(resp_str[body_start + 4:])
        except json.JSONDecodeError:
            pass
    return status_code, json_body


@pytest.mark.asyncio
async def test_asset_upload_raw_binary():
    """POST /api/assets with raw binary stores and returns URI."""
    with tempfile.TemporaryDirectory() as tmpdir:
        node = MockNode(asset_dir=tmpdir)
        server = CockpitServer(node, bind="127.0.0.1", port=0, auth_token="tok123")
        await server.start()
        try:
            data = b"hello world binary"
            expected_hash = hashlib.sha256(data).hexdigest()
            status, resp = await _raw_request(
                server.port, "/api/assets", body=data, token="tok123"
            )
            assert status == 200
            assert len(resp["assets"]) == 1
            assert resp["assets"][0]["hash"] == expected_hash
            assert resp["assets"][0]["uri"] == f"knarr-asset://{expected_hash}"
            assert resp["assets"][0]["size"] == len(data)
            # Verify file exists on disk
            assert os.path.isfile(os.path.join(tmpdir, expected_hash))
        finally:
            await server.stop()


@pytest.mark.asyncio
async def test_asset_upload_json_base64():
    """POST /api/assets with JSON base64 stores multiple files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        node = MockNode(asset_dir=tmpdir)
        server = CockpitServer(node, bind="127.0.0.1", port=0, auth_token="tok123")
        await server.start()
        try:
            file1 = b"file one"
            file2 = b"file two"
            payload = json.dumps({
                "files": [
                    {"data": base64.b64encode(file1).decode(), "name": "one.bin"},
                    {"data": base64.b64encode(file2).decode(), "name": "two.bin"},
                ]
            })
            status, resp = await _raw_request(
                server.port, "/api/assets", body=payload,
                content_type="application/json", token="tok123"
            )
            assert status == 200
            assert len(resp["assets"]) == 2
            assert resp["assets"][0]["name"] == "one.bin"
            assert resp["assets"][1]["name"] == "two.bin"
            assert resp["assets"][0]["uri"].startswith("knarr-asset://")
        finally:
            await server.stop()


@pytest.mark.asyncio
async def test_asset_upload_requires_auth():
    """POST /api/assets without auth token returns 401."""
    with tempfile.TemporaryDirectory() as tmpdir:
        node = MockNode(asset_dir=tmpdir)
        server = CockpitServer(node, bind="127.0.0.1", port=0, auth_token="tok123")
        await server.start()
        try:
            status, _ = await _raw_request(
                server.port, "/api/assets", body=b"data"
            )
            assert status == 401
        finally:
            await server.stop()


@pytest.mark.asyncio
async def test_asset_upload_no_sidecar():
    """POST /api/assets without sidecar returns 400."""
    node = MockNode(asset_dir="")  # no sidecar
    server = CockpitServer(node, bind="127.0.0.1", port=0, auth_token="tok123")
    await server.start()
    try:
        status, _ = await _raw_request(
            server.port, "/api/assets", body=b"data", token="tok123"
        )
        assert status == 400
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_asset_upload_empty_body():
    """POST /api/assets with empty body returns 400."""
    with tempfile.TemporaryDirectory() as tmpdir:
        node = MockNode(asset_dir=tmpdir)
        server = CockpitServer(node, bind="127.0.0.1", port=0, auth_token="tok123")
        await server.start()
        try:
            status, resp = await _raw_request(
                server.port, "/api/assets", body=b"", token="tok123"
            )
            assert status == 400
        finally:
            await server.stop()
