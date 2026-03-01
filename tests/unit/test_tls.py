"""Tests for Phase 9a: TLS cert generation and sidecar TLS."""
import asyncio
import hashlib
import os
import ssl
import time

import pytest
from nacl.signing import SigningKey

from knarr.mail.tls import (
    generate_tls_cert,
    resolve_cert_paths,
    create_server_ssl_context,
    create_client_ssl_context,
)
from knarr.dht.sidecar import AssetSidecar


@pytest.fixture
def signing_key():
    return SigningKey.generate()


@pytest.fixture
def node_identity(signing_key):
    key_bytes = bytes(signing_key)
    node_id = hashlib.sha256(signing_key.verify_key.encode()).hexdigest()
    return key_bytes, node_id


def test_cert_generation(tmp_path, node_identity):
    """Generate valid PEM files, cert CN contains node_id prefix."""
    key_bytes, node_id = node_identity
    cert_path, key_path = generate_tls_cert(key_bytes, node_id, str(tmp_path), force=True)

    assert os.path.exists(cert_path)
    assert os.path.exists(key_path)
    assert cert_path.endswith("cert.pem")
    assert key_path.endswith("key.pem")

    # Verify PEM format
    with open(cert_path, "rb") as f:
        cert_data = f.read()
    assert b"BEGIN CERTIFICATE" in cert_data

    with open(key_path, "rb") as f:
        key_data = f.read()
    assert b"BEGIN PRIVATE KEY" in key_data


def test_cert_from_existing_key(tmp_path, node_identity):
    """Cert's public key matches node's Ed25519 public key."""
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key_bytes, node_id = node_identity
    cert_path, key_path = generate_tls_cert(key_bytes, node_id, str(tmp_path), force=True)

    with open(cert_path, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())

    # CN should be first 16 chars of node_id
    cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
    assert cn == node_id[:16]

    # The cert's public key should match the identity key
    private_key = Ed25519PrivateKey.from_private_bytes(key_bytes)
    cert_pub_bytes = cert.public_key().public_bytes_raw()
    identity_pub_bytes = private_key.public_key().public_bytes_raw()
    assert cert_pub_bytes == identity_pub_bytes


def test_auto_cert_no_overwrite(tmp_path, node_identity):
    """Existing cert is not overwritten without force=True."""
    key_bytes, node_id = node_identity
    cert_path, key_path = generate_tls_cert(key_bytes, node_id, str(tmp_path), force=True)

    # Record file times
    cert_mtime = os.path.getmtime(cert_path)

    # Call again without force — should NOT overwrite
    cert_path2, key_path2 = generate_tls_cert(key_bytes, node_id, str(tmp_path), force=False)
    assert os.path.getmtime(cert_path2) == cert_mtime


def test_key_pem_permissions(tmp_path, node_identity):
    """key.pem should be chmod 0600."""
    key_bytes, node_id = node_identity
    _, key_path = generate_tls_cert(key_bytes, node_id, str(tmp_path), force=True)

    mode = os.stat(key_path).st_mode & 0o777
    assert mode == 0o600, f"Expected 0600, got {oct(mode)}"


def test_resolve_cert_paths_defaults(tmp_path):
    """Default paths are cert.pem and key.pem in config_dir."""
    cert, key = resolve_cert_paths({}, str(tmp_path))
    assert cert == os.path.join(str(tmp_path), "cert.pem")
    assert key == os.path.join(str(tmp_path), "key.pem")


def test_resolve_cert_paths_custom():
    """Custom paths from config are used."""
    config = {"network": {"tls_cert": "/custom/cert.pem", "tls_key": "/custom/key.pem"}}
    cert, key = resolve_cert_paths(config, "/ignored")
    assert cert == "/custom/cert.pem"
    assert key == "/custom/key.pem"


def test_server_ssl_context(tmp_path, node_identity):
    """Server SSL context loads cert chain successfully."""
    key_bytes, node_id = node_identity
    cert_path, key_path = generate_tls_cert(key_bytes, node_id, str(tmp_path), force=True)

    ctx = create_server_ssl_context(cert_path, key_path)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_client_ssl_context():
    """Client SSL context has CERT_NONE, no hostname check, TLS 1.2 floor."""
    ctx = create_client_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


# -- Sentinel: TLS mandatory --

@pytest.mark.asyncio
async def test_sidecar_tls_rejects_plaintext(tmp_path, signing_key):
    """SENTINEL: Sidecar with TLS rejects plaintext connections."""
    key_bytes = bytes(signing_key)
    node_id = hashlib.sha256(signing_key.verify_key.encode()).hexdigest()
    cert_path, key_path = generate_tls_cert(key_bytes, node_id, str(tmp_path), force=True)

    asset_dir = str(tmp_path / "assets")
    os.makedirs(asset_dir)
    sidecar = AssetSidecar(
        "127.0.0.1", 0, asset_dir, signing_key,
        cert_path=cert_path, key_path=key_path,
    )
    await sidecar.start()
    try:
        # Plaintext connection should fail (TLS is mandatory)
        reader, writer = await asyncio.open_connection("127.0.0.1", sidecar.port)
        writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        # Should get connection reset or empty response
        data = await asyncio.wait_for(reader.read(1024), timeout=2.0)
        # TLS server will either close connection or send garbage to plaintext client
        # Either way, it should NOT be a valid HTTP response
        assert b"200 OK" not in data
        writer.close()
        await writer.wait_closed()
    except (ConnectionResetError, ConnectionError, asyncio.TimeoutError, ssl.SSLError):
        pass  # Expected — plaintext rejected
    finally:
        await sidecar.stop()


@pytest.mark.asyncio
async def test_sidecar_tls_roundtrip(tmp_path, signing_key):
    """Upload and download over TLS works."""
    key_bytes = bytes(signing_key)
    node_id = hashlib.sha256(signing_key.verify_key.encode()).hexdigest()
    cert_path, key_path = generate_tls_cert(key_bytes, node_id, str(tmp_path), force=True)

    asset_dir = str(tmp_path / "assets")
    os.makedirs(asset_dir)
    sidecar = AssetSidecar(
        "127.0.0.1", 0, asset_dir, signing_key,
        cert_path=cert_path, key_path=key_path,
    )
    await sidecar.start()
    try:
        data = b"test tls upload"
        content_hash = hashlib.sha256(data).hexdigest()
        pub_key_hex = signing_key.verify_key.encode().hex()
        timestamp = str(int(time.time()))

        payload = f"PUT:/assets:{timestamp}:{content_hash}".encode("utf-8")
        signature = signing_key.sign(payload).signature.hex()

        ssl_ctx = create_client_ssl_context()

        # Upload over TLS
        reader, writer = await asyncio.open_connection("127.0.0.1", sidecar.port, ssl=ssl_ctx)
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

        # Download over TLS
        payload_get = f"GET:/assets/{content_hash}:{timestamp}:empty".encode("utf-8")
        signature_get = signing_key.sign(payload_get).signature.hex()

        reader, writer = await asyncio.open_connection("127.0.0.1", sidecar.port, ssl=ssl_ctx)
        req = (
            f"GET /assets/{content_hash} HTTP/1.1\r\n"
            f"x-knarr-publickey: {pub_key_hex}\r\n"
            f"x-knarr-signature: {signature_get}\r\n"
            f"x-knarr-timestamp: {timestamp}\r\n\r\n"
        ).encode()
        writer.write(req)
        resp_line = await reader.readline()
        assert b"200 OK" in resp_line
        while True:
            line = await reader.readline()
            if line == b"\r\n":
                break
        body = await reader.read()
        assert body == data
    finally:
        await sidecar.stop()
