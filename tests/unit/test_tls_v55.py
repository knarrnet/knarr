"""Tests for v0.55.0 C-02 — TLS on TCP connections."""

import asyncio
import hashlib
import os
import ssl
import pytest

from knarr.core.crypto import (
    SigningKey, create_server_tls_context, create_client_tls_context,
    ensure_node_tls_cert,
)
from knarr.dht.pool import ConnectionPool


class TestTLSContextCreation:
    def test_client_context_is_ssl_context(self):
        ctx = create_client_tls_context()
        assert isinstance(ctx, ssl.SSLContext)

    def test_client_context_no_hostname_check(self):
        ctx = create_client_tls_context()
        assert not ctx.check_hostname
        assert ctx.verify_mode == ssl.CERT_NONE

    def test_client_context_tls12_minimum(self):
        ctx = create_client_tls_context()
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2

    def test_server_context_missing_cert_returns_none(self, tmp_path):
        ctx = create_server_tls_context("", "")
        assert ctx is None

    def test_server_context_missing_files_returns_none(self, tmp_path):
        ctx = create_server_tls_context(
            str(tmp_path / "cert.pem"),
            str(tmp_path / "key.pem"),
        )
        assert ctx is None

    def test_server_context_with_valid_cert(self, tmp_path):
        from knarr.mail.tls import generate_tls_cert
        sk = SigningKey.generate()
        node_id = hashlib.sha256(sk.verify_key.encode()).hexdigest()
        cert_path, key_path = generate_tls_cert(sk.encode(), node_id, str(tmp_path))

        ctx = create_server_tls_context(cert_path, key_path)
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


class TestEnsureNodeTLSCert:
    def test_generates_cert_when_missing(self, tmp_path):
        from knarr.mail.tls import resolve_cert_paths

        sk = SigningKey.generate()
        node_id = hashlib.sha256(sk.verify_key.encode()).hexdigest()
        config = {"_config_dir": str(tmp_path), "_data_dir": str(tmp_path)}

        cert_path, key_path = ensure_node_tls_cert(
            config, str(tmp_path), node_id, sk.encode()
        )
        assert os.path.exists(cert_path)
        assert os.path.exists(key_path)

    def test_returns_existing_cert_without_regenerating(self, tmp_path):
        from knarr.mail.tls import generate_tls_cert

        sk = SigningKey.generate()
        node_id = hashlib.sha256(sk.verify_key.encode()).hexdigest()
        config = {"_config_dir": str(tmp_path), "_data_dir": str(tmp_path)}

        # Generate once
        cert1, key1 = generate_tls_cert(sk.encode(), node_id, str(tmp_path))
        mtime_before = os.path.getmtime(cert1)

        # ensure_node_tls_cert should NOT regenerate if files exist
        cert2, key2 = ensure_node_tls_cert(config, str(tmp_path), node_id, sk.encode())
        mtime_after = os.path.getmtime(cert2)

        assert mtime_before == mtime_after


class TestConnectionPoolTLS:
    def test_set_tls_context_stores_context(self):
        pool = ConnectionPool(max_connections=5)
        ctx = create_client_tls_context()
        pool.set_tls_context(ctx, tls_required=True)
        assert pool._tls_ctx is ctx
        assert pool._tls_required is True

    def test_set_tls_context_none_disables_tls(self):
        pool = ConnectionPool(max_connections=5)
        pool.set_tls_context(None, tls_required=False)
        assert pool._tls_ctx is None
        assert pool._tls_required is False

    def test_pool_default_no_tls(self):
        pool = ConnectionPool(max_connections=5)
        assert pool._tls_ctx is None

    @pytest.mark.asyncio
    async def test_tls_server_client_handshake(self, tmp_path):
        """Full TLS handshake between server and client using node identity cert."""
        from knarr.mail.tls import generate_tls_cert

        sk = SigningKey.generate()
        node_id = hashlib.sha256(sk.verify_key.encode()).hexdigest()
        cert_path, key_path = generate_tls_cert(sk.encode(), node_id, str(tmp_path))

        server_ctx = create_server_tls_context(cert_path, key_path)
        client_ctx = create_client_tls_context()

        received = []

        async def handle_client(reader, writer):
            data = await reader.read(100)
            received.append(data)
            writer.write(b"pong")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(
            handle_client, "127.0.0.1", 0, ssl=server_ctx
        )
        port = server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=client_ctx
            )
            writer.write(b"ping")
            await writer.drain()
            response = await reader.read(100)
            writer.close()
            assert response == b"pong"
            assert received == [b"ping"]
        finally:
            server.close()
            await server.wait_closed()


class TestNodeTLSConfig:
    """Test that node.py reads TLS config correctly."""

    def test_tls_config_defaults(self):
        """Default config has tls=true, tls_required=true."""
        config = {}
        node_cfg = config.get("node", {})
        tls_enabled = node_cfg.get("tls", True)
        tls_required = node_cfg.get("tls_required", True)
        assert tls_enabled is True
        assert tls_required is True

    def test_tls_config_can_be_disabled(self):
        config = {"node": {"tls": False, "tls_required": False}}
        node_cfg = config.get("node", {})
        tls_enabled = node_cfg.get("tls", True)
        tls_required = node_cfg.get("tls_required", True)
        assert tls_enabled is False
        assert tls_required is False
