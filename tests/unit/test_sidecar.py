import pytest
import os
import time
import shutil
import asyncio
import json
import hashlib
from nacl.signing import SigningKey
from knarr.dht.sidecar import AssetSidecar, TaskContext, AssetMetadata

@pytest.fixture
def asset_dir(tmp_path):
    d = tmp_path / "assets"
    d.mkdir()
    return str(d)

@pytest.fixture
def signing_key():
    return SigningKey.generate()

def test_store_and_get_asset(asset_dir):
    ctx = TaskContext(asset_dir)
    data = b"hello world"
    h = ctx.store_asset(data)
    assert len(h) == 64
    read_back = ctx.get_asset(h)
    assert read_back == data

def test_store_asset_dedup(asset_dir):
    ctx = TaskContext(asset_dir)
    data = b"same content"
    h1 = ctx.store_asset(data)
    h2 = ctx.store_asset(data)
    assert h1 == h2
    # Verify file exists
    assert os.path.exists(os.path.join(asset_dir, h1))

def test_store_asset_atomic(asset_dir):
    ctx = TaskContext(asset_dir)
    data = b"atomic"
    h = ctx.store_asset(data)
    # Check for tmp file
    assert not os.path.exists(os.path.join(asset_dir, h + ".tmp"))

def test_get_asset_not_found(asset_dir):
    ctx = TaskContext(asset_dir)
    with pytest.raises(FileNotFoundError):
        ctx.get_asset("a" * 64)

def test_validate_hash_rejects_bad_input(asset_dir):
    ctx = TaskContext(asset_dir)
    with pytest.raises(ValueError):
        ctx._validate_hash("../etc/passwd")
    with pytest.raises(ValueError):
        ctx._validate_hash("A" * 64) # Uppercase
    with pytest.raises(ValueError):
        ctx._validate_hash("abc")

@pytest.mark.asyncio
async def test_sidecar_upload_download_roundtrip(asset_dir, signing_key):
    sidecar = AssetSidecar("127.0.0.1", 0, asset_dir, signing_key)
    await sidecar.start()
    try:
        data = b"test upload"
        content_hash = hashlib.sha256(data).hexdigest()
        
        # Upload
        pub_key_hex = signing_key.verify_key.encode().hex()
        timestamp = str(int(time.time()))
        payload = f"PUT:/assets:{timestamp}:{content_hash}".encode("utf-8")
        signature = signing_key.sign(payload).signature.hex()
        
        reader, writer = await asyncio.open_connection("127.0.0.1", sidecar.port)
        headers = (
            f"PUT /assets HTTP/1.1\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"x-knarr-publickey: {pub_key_hex}\r\n"
            f"x-knarr-signature: {signature}\r\n"
            f"x-knarr-timestamp: {timestamp}\r\n"
            f"x-knarr-content-hash: {content_hash}\r\n\r\n"
        ).encode()
        writer.write(headers + data)
        
        resp_line = await reader.readline()
        assert b"200 OK" in resp_line
        writer.close()
        await writer.wait_closed()
        
        # Download
        reader, writer = await asyncio.open_connection("127.0.0.1", sidecar.port)
        
        # Sign the GET request
        payload_get = f"GET:/assets/{content_hash}:{timestamp}:empty".encode("utf-8")
        signature_get = signing_key.sign(payload_get).signature.hex()
        
        req = (
            f"GET /assets/{content_hash} HTTP/1.1\r\n"
            f"x-knarr-publickey: {pub_key_hex}\r\n"
            f"x-knarr-signature: {signature_get}\r\n"
            f"x-knarr-timestamp: {timestamp}\r\n\r\n"
        ).encode()
        
        writer.write(req)
        resp_line = await reader.readline()
        assert b"200 OK" in resp_line
        
        # Skip headers
        while True:
            line = await reader.readline()
            if line == b"\r\n": break
            
        body = await reader.read()
        assert body == data
        
    finally:
        await sidecar.stop()

@pytest.mark.asyncio
async def test_sidecar_auth_rejection(asset_dir):
    sidecar = AssetSidecar("127.0.0.1", 0, asset_dir)
    await sidecar.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", sidecar.port)
        writer.write(b"PUT /assets HTTP/1.1\r\nContent-Length: 5\r\n\r\nhello")
        resp = await reader.readline()
        assert b"401 Unauthorized" in resp
    finally:
        await sidecar.stop()

@pytest.mark.asyncio
async def test_sidecar_auth_replay_rejection(asset_dir, signing_key):
    sidecar = AssetSidecar("127.0.0.1", 0, asset_dir, signing_key)
    await sidecar.start()
    try:
        old_ts = str(int(time.time()) - 120)
        pub_key_hex = signing_key.verify_key.encode().hex()
        payload = f"PUT:/assets:{old_ts}:empty".encode("utf-8")
        signature = signing_key.sign(payload).signature.hex()
        
        reader, writer = await asyncio.open_connection("127.0.0.1", sidecar.port)
        headers = (
            f"PUT /assets HTTP/1.1\r\n"
            f"Content-Length: 0\r\n"
            f"x-knarr-publickey: {pub_key_hex}\r\n"
            f"x-knarr-signature: {signature}\r\n"
            f"x-knarr-timestamp: {old_ts}\r\n\r\n"
        ).encode()
        writer.write(headers)
        resp = await reader.readline()
        assert b"401 Unauthorized" in resp
    finally:
        await sidecar.stop()

@pytest.mark.asyncio
async def test_sidecar_size_limit(asset_dir, signing_key):
    sidecar = AssetSidecar("127.0.0.1", 0, asset_dir, signing_key, max_asset_size=10)
    await sidecar.start()
    try:
        pub_key_hex = signing_key.verify_key.encode().hex()
        ts = str(int(time.time()))
        # Need to sign correctly to get past auth
        payload = f"PUT:/assets:{ts}:empty".encode("utf-8")
        signature = signing_key.sign(payload).signature.hex()
        
        reader, writer = await asyncio.open_connection("127.0.0.1", sidecar.port)
        headers = (
            f"PUT /assets HTTP/1.1\r\n"
            f"Content-Length: 20\r\n"
            f"x-knarr-publickey: {pub_key_hex}\r\n"
            f"x-knarr-signature: {signature}\r\n"
            f"x-knarr-timestamp: {ts}\r\n\r\n"
        ).encode()
        writer.write(headers)
        resp = await reader.readline()
        assert b"413" in resp
    finally:
        await sidecar.stop()

@pytest.mark.asyncio
async def test_sidecar_delete_provider_only(asset_dir, signing_key):
    # Setup: create file
    path = os.path.join(asset_dir, "a"*64)
    with open(path, "wb") as f: f.write(b"data")
    
    sidecar = AssetSidecar("127.0.0.1", 0, asset_dir, signing_key)
    await sidecar.start()
    try:
        # Provider key matches sidecar init key
        pub_key_hex = signing_key.verify_key.encode().hex()
        ts = str(int(time.time()))
        payload = f"DELETE:/assets/{'a'*64}:{ts}:empty".encode("utf-8")
        signature = signing_key.sign(payload).signature.hex()
        
        reader, writer = await asyncio.open_connection("127.0.0.1", sidecar.port)
        headers = (
            f"DELETE /assets/{'a'*64} HTTP/1.1\r\n"
            f"x-knarr-publickey: {pub_key_hex}\r\n"
            f"x-knarr-signature: {signature}\r\n"
            f"x-knarr-timestamp: {ts}\r\n\r\n"
        ).encode()
        writer.write(headers)
        resp = await reader.readline()
        assert b"200 OK" in resp
        
        # Verify deleted
        assert not os.path.exists(path)

    finally:
        await sidecar.stop()


def test_store_asset_syncs_sidecar_metadata(asset_dir, signing_key):
    """V010-002 sentinel: store_asset via node syncs sidecar metadata."""
    from unittest.mock import MagicMock
    from knarr.dht.node import DHTNode
    import time as _time

    # Build a minimal mock that exercises the real store_asset method
    node = object.__new__(DHTNode)
    node._asset_dir = asset_dir
    sidecar = AssetSidecar("127.0.0.1", 0, asset_dir, signing_key,
                           max_asset_size=1024, max_total_size=2048)
    node._sidecar = sidecar

    data = b"test accounting data"
    h = node.store_asset(data)

    assert h in sidecar._metadata
    assert sidecar._metadata[h].size == len(data)
    assert sidecar._total_size == len(data)


def test_store_asset_rejects_over_quota(asset_dir, signing_key):
    """V010-002 sentinel: store_asset respects sidecar size limits."""
    from knarr.dht.node import DHTNode

    node = object.__new__(DHTNode)
    node._asset_dir = asset_dir
    sidecar = AssetSidecar("127.0.0.1", 0, asset_dir, signing_key,
                           max_asset_size=10, max_total_size=100)
    node._sidecar = sidecar

    with pytest.raises(ValueError, match="exceeds max size"):
        node.store_asset(b"x" * 20)