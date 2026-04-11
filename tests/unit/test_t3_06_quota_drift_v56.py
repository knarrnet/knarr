import asyncio
import hashlib
import os
import time

import pytest
from nacl.signing import SigningKey

from knarr.dht.sidecar import AssetSidecar, KNARR_ASSET_ENCRYPTION_OVERHEAD


class _FakeVault:
    def encrypt_bytes(self, data: bytes) -> bytes:
        return data + (b"x" * KNARR_ASSET_ENCRYPTION_OVERHEAD)

    def decrypt_bytes(self, data: bytes) -> bytes:
        return data[:-KNARR_ASSET_ENCRYPTION_OVERHEAD]


async def _upload(sidecar: AssetSidecar, signing_key: SigningKey, data: bytes) -> bytes:
    content_hash = hashlib.sha256(data).hexdigest()
    timestamp = str(int(time.time()))
    pub_key_hex = signing_key.verify_key.encode().hex()
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
    ).encode("utf-8")
    writer.write(headers + data)
    status_line = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return status_line


@pytest.mark.asyncio
async def test_sidecar_quota_accounts_for_secretbox_overhead(tmp_path):
    sidecar = AssetSidecar(
        "127.0.0.1",
        0,
        str(tmp_path / "assets"),
        SigningKey.generate(),
        max_total_size=50,
        vault=_FakeVault(),
    )
    await sidecar.start()
    try:
        data = b"12345678901234567890"
        status_line = await _upload(sidecar, sidecar._signing_key, data)

        assert b"507" in status_line
        assert sidecar._metadata == {}
        assert sidecar._total_size == 0
    finally:
        await sidecar.stop()


@pytest.mark.asyncio
async def test_sidecar_metadata_tracks_encrypted_stored_size(tmp_path):
    asset_dir = tmp_path / "assets"
    sidecar = AssetSidecar(
        "127.0.0.1",
        0,
        str(asset_dir),
        SigningKey.generate(),
        max_total_size=1024,
        vault=_FakeVault(),
    )
    await sidecar.start()
    try:
        data = b"encrypted-asset"
        content_hash = hashlib.sha256(data).hexdigest()
        status_line = await _upload(sidecar, sidecar._signing_key, data)

        assert b"200 OK" in status_line
        assert sidecar._metadata[content_hash].size == len(data) + KNARR_ASSET_ENCRYPTION_OVERHEAD
        assert sidecar._total_size == len(data) + KNARR_ASSET_ENCRYPTION_OVERHEAD
        assert os.path.getsize(asset_dir / content_hash) == len(data) + KNARR_ASSET_ENCRYPTION_OVERHEAD
    finally:
        await sidecar.stop()
