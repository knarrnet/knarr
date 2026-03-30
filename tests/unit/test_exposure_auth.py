"""Tests for token-gated exposures (CR-005)."""
import asyncio
import json
import pytest
from knarr.dashboard.server import CockpitServer


class _MockNodeInfo:
    node_id = "mock" * 16
    port = 9000


class MockNode:
    def get_status(self): return {"status": "ok"}
    def get_peers(self): return []
    def get_skills(self): return {"local": [], "network": []}
    def get_tasks(self): return []
    def get_ledger(self): return []
    _handlers = {"my-skill": (None, False)}
    node_info = _MockNodeInfo()

    def __init__(self):
        from unittest.mock import MagicMock
        self._base_storage = MagicMock()
        self.storage = self._base_storage
        self._base_bus = None
        self._base_signing_key = None
        self._base_public_key_hex = ""

    def get_skill_schema(self, name):
        return {"input_schema": {"text": "string"}} if name == "my-skill" else None

    async def call_local(self, skill, input_data, **kwargs):
        return {"result": "ok"}


def _make_server(auth="none", tokens=None, max_per_token=0, max_per_day=0):
    exposures = {
        "test-exp": {
            "skill": "my-skill",
            "path": "test-exp",
            "enabled": True,
            "fields": {"text": {"label": "Text"}},
            "presets": {},
            "display": {},
            "provider": {},
            "rate_limit": 100,
            "auth": auth,
            "tokens": tokens or [],
            "max_calls_per_token": max_per_token,
            "max_calls_per_day": max_per_day,
        },
    }
    return CockpitServer(MockNode(), bind="127.0.0.1", port=0, exposures=exposures)


async def _request(port, path, body=None, token=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    method = "POST" if body else "GET"
    headers = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
    if token:
        headers += f"Authorization: Bearer {token}\r\n"
    if body:
        data = json.dumps(body).encode()
        headers += f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n"
    headers += "\r\n"
    writer.write(headers.encode())
    if body:
        writer.write(data)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), timeout=5.0)
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return response


@pytest.mark.asyncio
async def test_exposure_no_auth_allows_execute():
    """Exposure with auth=none allows unauthenticated execute."""
    server = _make_server(auth="none")
    await server.start()
    try:
        resp = await _request(server.port, "/s/test-exp/execute", body={"text": "hi"})
        # Local skill execution now returns 202 Accepted (fire-and-forget)
        assert b"200 OK" in resp or b"202 Accepted" in resp
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exposure_token_auth_rejects_without_token():
    """Exposure with auth=token rejects requests without Bearer token."""
    server = _make_server(auth="token", tokens=["secret123"])
    await server.start()
    try:
        resp = await _request(server.port, "/s/test-exp/execute", body={"text": "hi"})
        assert b"403 Forbidden" in resp
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exposure_token_auth_accepts_valid_token():
    """Exposure with auth=token accepts valid Bearer token."""
    server = _make_server(auth="token", tokens=["secret123"])
    await server.start()
    try:
        resp = await _request(server.port, "/s/test-exp/execute",
                              body={"text": "hi"}, token="secret123")
        # Local skill execution now returns 202 Accepted (fire-and-forget)
        assert b"200 OK" in resp or b"202 Accepted" in resp
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exposure_token_auth_rejects_wrong_token():
    """Exposure with auth=token rejects wrong Bearer token."""
    server = _make_server(auth="token", tokens=["secret123"])
    await server.start()
    try:
        resp = await _request(server.port, "/s/test-exp/execute",
                              body={"text": "hi"}, token="wrong")
        assert b"403 Forbidden" in resp
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exposure_per_day_rate_limit():
    """Exposure with max_calls_per_day enforces daily limit."""
    server = _make_server(auth="none", max_per_day=2)
    await server.start()
    try:
        # First two should succeed (local skills now return 202 Accepted)
        resp1 = await _request(server.port, "/s/test-exp/execute", body={"text": "1"})
        assert b"200 OK" in resp1 or b"202 Accepted" in resp1
        resp2 = await _request(server.port, "/s/test-exp/execute", body={"text": "2"})
        assert b"200 OK" in resp2 or b"202 Accepted" in resp2
        # Third should be rate-limited
        resp3 = await _request(server.port, "/s/test-exp/execute", body={"text": "3"})
        assert b"429" in resp3
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exposure_token_auth_empty_tokens_rejects():
    """V010-001 sentinel: auth=token with empty tokens list must reject (fail closed)."""
    server = _make_server(auth="token", tokens=[])
    await server.start()
    try:
        resp = await _request(server.port, "/s/test-exp/execute", body={"text": "hi"})
        assert b"403 Forbidden" in resp
    finally:
        await server.stop()
