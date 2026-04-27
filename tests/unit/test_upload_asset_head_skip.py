"""B-08: upload_asset skips PUT when sidecar HEAD returns 200.

Observable contract:
  - HEAD-hit (sidecar already has the bytes) → no PUT, returns hash.
  - HEAD-miss (404, transport error, anything non-200) → PUT executes.
  - HEAD request is signed with the same scheme as GET/PUT.
"""
import hashlib
from unittest.mock import AsyncMock, patch

import pytest
from nacl.signing import SigningKey

from knarr.cli import main as cli_main


class _FakeWriter:
    def __init__(self, sink: list):
        self._sink = sink
        self.closed = False

    def write(self, data):
        self._sink.append(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None

    def get_extra_info(self, _key):
        return None


class _FakeReader:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""

    async def read(self, _n=-1):
        return b""


@pytest.mark.asyncio
async def test_upload_skips_put_when_head_returns_200():
    sk = SigningKey.generate()
    data = b"hello world"
    expected_hash = hashlib.sha256(data).hexdigest()

    calls = []

    async def fake_open(host, port, ssl=None):
        # First call is HEAD, respond 200. If a second call is made, the
        # skip didn't happen — fail the test via an assertion later.
        calls.append((host, port))
        return _FakeReader([b"HTTP/1.1 200 OK\r\n", b"\r\n"]), _FakeWriter([])

    with patch.object(cli_main.asyncio, "open_connection", side_effect=fake_open), \
         patch.object(cli_main, "create_client_ssl_context", return_value=None, create=True):
        got = await cli_main.upload_asset("127.0.0.1", 9031, data, sk)

    assert got == expected_hash
    assert len(calls) == 1, f"expected single HEAD connection, got {len(calls)}"


@pytest.mark.asyncio
async def test_upload_falls_back_to_put_when_head_misses():
    sk = SigningKey.generate()
    data = b"bytes-for-put"
    expected_hash = hashlib.sha256(data).hexdigest()

    sequence = [
        (_FakeReader([b"HTTP/1.1 404 Not Found\r\n", b"\r\n"]), _FakeWriter([])),
        (_FakeReader([b"HTTP/1.1 200 OK\r\n", b"\r\n"]), _FakeWriter([])),
    ]

    async def fake_open(host, port, ssl=None):
        return sequence.pop(0)

    with patch.object(cli_main.asyncio, "open_connection", side_effect=fake_open), \
         patch.object(cli_main, "create_client_ssl_context", return_value=None, create=True):
        got = await cli_main.upload_asset("127.0.0.1", 9031, data, sk)

    assert got == expected_hash
    assert sequence == [], "PUT connection should have been consumed"


@pytest.mark.asyncio
async def test_upload_falls_back_to_put_on_head_transport_error():
    sk = SigningKey.generate()
    data = b"bytes-b-08"
    expected_hash = hashlib.sha256(data).hexdigest()

    attempts = {"n": 0}

    async def fake_open(host, port, ssl=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("connection refused on HEAD")
        return _FakeReader([b"HTTP/1.1 200 OK\r\n", b"\r\n"]), _FakeWriter([])

    with patch.object(cli_main.asyncio, "open_connection", side_effect=fake_open), \
         patch.object(cli_main, "create_client_ssl_context", return_value=None, create=True):
        got = await cli_main.upload_asset("127.0.0.1", 9031, data, sk)

    assert got == expected_hash
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_sidecar_has_asset_signs_head_request():
    sk = SigningKey.generate()
    content_hash = "a" * 64
    sink = []

    async def fake_open(host, port, ssl=None):
        return _FakeReader([b"HTTP/1.1 200 OK\r\n", b"\r\n"]), _FakeWriter(sink)

    with patch.object(cli_main.asyncio, "open_connection", side_effect=fake_open), \
         patch.object(cli_main, "create_client_ssl_context", return_value=None, create=True):
        ok = await cli_main._sidecar_has_asset("127.0.0.1", 9031, content_hash, sk)

    assert ok is True
    request = b"".join(sink).decode("utf-8", errors="replace")
    assert request.startswith("HEAD /assets/" + content_hash)
    assert "x-knarr-publickey: " in request
    assert "x-knarr-signature: " in request
    assert "x-knarr-timestamp: " in request
