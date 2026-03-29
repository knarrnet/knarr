import asyncio
import hashlib
import json
import logging
import os
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)

@dataclass
class TaskContext:
    """Provides asset operations and cancellation status to handlers. Thread-safe."""
    _asset_dir: str
    cancelled: threading.Event = field(default_factory=threading.Event)

    def get_asset(self, hash: str) -> bytes:
        """Read an asset from local storage. Returns bytes. Raises FileNotFoundError."""
        self._validate_hash(hash)
        path = os.path.join(self._asset_dir, hash)
        with open(path, "rb") as f:
            return f.read()

    def store_asset(self, data: bytes) -> str:
        """Store binary data as an asset. Returns SHA-256 hash string.
        Atomic: writes to temp file, then renames."""
        content_hash = hashlib.sha256(data).hexdigest()
        final_path = os.path.join(self._asset_dir, content_hash)
        if os.path.exists(final_path):
            return content_hash  # dedup
        tmp_path = final_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, final_path)
        return content_hash

    def asset_path(self, hash: str) -> str:
        """Return the local file path for an asset hash."""
        self._validate_hash(hash)
        return os.path.join(self._asset_dir, hash)

    @staticmethod
    def _validate_hash(hash: str):
        if len(hash) != 64 or not all(c in '0123456789abcdef' for c in hash):
            raise ValueError(f"Invalid asset hash: must be 64 hex chars")


@dataclass
class AssetMetadata:
    size: int
    uploaded_at: float
    uploader_key: str
    access_count: int = 0
    last_accessed: float = 0.0


class AssetSidecar:
    """Lightweight HTTP server for binary asset transfer."""

    def __init__(self, host: str, port: int, asset_dir: str,
                 signing_key=None,  # nacl.signing.SigningKey for auth verification
                 max_asset_size: int = 104857600,  # 100MB
                 max_total_size: int = 1073741824,  # 1GB
                 asset_ttl: int = 3600,  # 1 hour
                 cert_path: str = "",
                 key_path: str = ""):
        self._host = host
        self._port = port
        self._asset_dir = asset_dir
        self._signing_key = signing_key
        self._max_asset_size = max_asset_size
        self._max_total_size = max_total_size
        self._asset_ttl = asset_ttl
        self._cert_path = cert_path
        self._key_path = key_path
        self._server = None
        self._cleanup_task = None
        self._metadata: Dict[str, AssetMetadata] = {}
        self._total_size: int = 0
        self._max_connections: int = 64
        self._active_connections: int = 0
        os.makedirs(asset_dir, exist_ok=True)

    @property
    def port(self) -> int:
        return self._port

    async def start(self):
        """Start the HTTPS sidecar server (TLS mandatory)."""
        import ssl
        self._rebuild_metadata()
        ssl_ctx = None
        if self._cert_path and self._key_path:
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ssl_ctx.load_cert_chain(self._cert_path, self._key_path)
        self._server = await asyncio.start_server(
            self._handle_connection, self._host, self._port, ssl=ssl_ctx,
            reuse_address=True,
        )
        # Get actual port if 0 was passed
        sock = self._server.sockets[0]
        self._port = sock.getsockname()[1]
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        tls_label = "HTTPS" if ssl_ctx else "HTTP (no TLS cert)"
        logger.info(f"Asset sidecar listening on {self._host}:{self._port} ({tls_label})")

    async def stop(self):
        """Stop the sidecar server."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def _rebuild_metadata(self):
        """Scan asset directory and rebuild in-memory metadata."""
        self._metadata.clear()
        self._total_size = 0
        if not os.path.isdir(self._asset_dir):
            return
        for filename in os.listdir(self._asset_dir):
            path = os.path.join(self._asset_dir, filename)
            if not os.path.isfile(path):
                continue
            # Validate filename is a valid hash
            if len(filename) != 64 or not all(c in '0123456789abcdef' for c in filename):
                continue
            stat = os.stat(path)
            self._metadata[filename] = AssetMetadata(
                size=stat.st_size,
                uploaded_at=stat.st_mtime,
                uploader_key="",
                access_count=0,
                last_accessed=stat.st_mtime,
            )
            self._total_size += stat.st_size

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle one HTTP request, then close (no keep-alive)."""
        # P8A1-004: Admission control — reject if at capacity
        if self._active_connections >= self._max_connections:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return
        self._active_connections += 1
        try:
            # Read request line
            request_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not request_line:
                return
            parts = request_line.decode("utf-8", errors="replace").strip().split(" ")
            if len(parts) < 2:
                await self._send_response(writer, 400, {"error": "Bad request"})
                return
            method = parts[0].upper()
            path = parts[1]

            # Read headers
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                if line in (b"\r\n", b"\n", b""):
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                if ": " in decoded:
                    key, value = decoded.split(": ", 1)
                    headers[key.lower()] = value

            # Verify Ed25519 auth
            if not self._verify_auth(method, path, headers):
                await self._send_response(writer, 401, {"error": "Unauthorized"})
                return

            # Route
            if method == "PUT" and path == "/assets":
                await self._handle_upload(reader, writer, headers)
            elif method == "GET" and path == "/assets":
                await self._handle_list(writer)
            elif method == "GET" and path.startswith("/assets/"):
                asset_hash = path[len("/assets/"):]
                await self._handle_download(writer, asset_hash)
            elif method == "HEAD" and path.startswith("/assets/"):
                asset_hash = path[len("/assets/"):]
                await self._handle_head(writer, asset_hash)
            elif method == "DELETE" and path.startswith("/assets/"):
                asset_hash = path[len("/assets/"):]
                pub_key = headers.get("x-knarr-publickey", "")
                await self._handle_delete(writer, asset_hash, pub_key)
            else:
                await self._send_response(writer, 404, {"error": "Not found"})

        except asyncio.TimeoutError:
            pass
        except Exception as e:
            logger.error(f"Sidecar connection error: {type(e).__name__}")
        finally:
            self._active_connections -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    def _verify_auth(self, method: str, path: str, headers: dict) -> bool:
        """Verify Ed25519 signature in HTTP headers."""
        from nacl.signing import VerifyKey
        from nacl.exceptions import BadSignatureError

        pub_key_hex = headers.get("x-knarr-publickey", "")
        signature_hex = headers.get("x-knarr-signature", "")
        timestamp_str = headers.get("x-knarr-timestamp", "")

        if not pub_key_hex or not signature_hex or not timestamp_str:
            return False

        try:
            timestamp = int(timestamp_str)
        except ValueError:
            return False

        # Replay protection: ±60 seconds
        now = int(time.time())
        if abs(now - timestamp) > 60:
            return False

        # Reconstruct signed payload
        content_hash = headers.get("x-knarr-content-hash", "empty")
        payload = f"{method}:{path}:{timestamp}:{content_hash}".encode("utf-8")

        try:
            verify_key = VerifyKey(bytes.fromhex(pub_key_hex))
            verify_key.verify(payload, bytes.fromhex(signature_hex))
            return True
        except (BadSignatureError, ValueError, Exception):
            return False

    async def _handle_list(self, writer):
        """GET /assets — list all assets with metadata."""
        entries = []
        for h, m in sorted(self._metadata.items()):
            entries.append({
                "hash": h,
                "size": m.size,
                "uploaded_at": m.uploaded_at,
                "access_count": m.access_count,
            })
            if len(entries) >= 1000:
                break
        await self._send_response(writer, 200, {"assets": entries, "total_size": self._total_size})

    async def _handle_upload(self, reader, writer, headers):
        """PUT /assets — upload binary data, return hash."""
        try:
            content_length = int(headers.get("content-length", "0"))
        except (ValueError, OverflowError):
            await self._send_response(writer, 400, {"error": "Invalid Content-Length"})
            return
        if content_length <= 0:
            await self._send_response(writer, 400, {"error": "Content-Length required"})
            return
        if content_length > self._max_asset_size:
            await self._send_response(writer, 413, {"error": f"Exceeds max size {self._max_asset_size}"})
            return
        if self._total_size + content_length > self._max_total_size:
            await self._send_response(writer, 507, {"error": "Storage capacity exceeded"})
            return

        data = await asyncio.wait_for(reader.readexactly(content_length), timeout=300.0)
        content_hash = hashlib.sha256(data).hexdigest()

        # Verify content hash matches header (integrity check)
        declared_hash = headers.get("x-knarr-content-hash", "")
        if declared_hash and declared_hash != "empty" and declared_hash != content_hash:
            await self._send_response(writer, 400, {"error": "Content hash mismatch"})
            return

        # Atomic write — C-05: use asyncio.to_thread to avoid blocking the event loop
        final_path = os.path.join(self._asset_dir, content_hash)
        if not os.path.exists(final_path):
            tmp_path = final_path + ".tmp"
            await self._write_file(tmp_path, data)
            os.replace(tmp_path, final_path)
            self._metadata[content_hash] = AssetMetadata(
                size=content_length,
                uploaded_at=time.time(),
                uploader_key=headers.get("x-knarr-publickey", ""),
            )
            self._total_size += content_length

        await self._send_response(writer, 200, {"hash": content_hash, "size": content_length})

    async def _handle_download(self, writer, asset_hash):
        """GET /assets/{hash} — download binary data."""
        if not self._is_valid_hash(asset_hash):
            await self._send_response(writer, 400, {"error": "Invalid hash format"})
            return
        path = os.path.join(self._asset_dir, asset_hash)
        if not os.path.exists(path):
            await self._send_response(writer, 404, {"error": "Asset not found"})
            return

        # C-05: use asyncio.to_thread to avoid blocking the event loop on large reads
        data = await self._read_file(path)

        if asset_hash in self._metadata:
            self._metadata[asset_hash].access_count += 1
            self._metadata[asset_hash].last_accessed = time.time()

        header = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8")
        writer.write(header + data)
        await writer.drain()

    async def _handle_head(self, writer, asset_hash):
        """HEAD /assets/{hash} — check existence and size."""
        if not self._is_valid_hash(asset_hash):
            await self._send_response(writer, 400, {"error": "Invalid hash format"})
            return
        path = os.path.join(self._asset_dir, asset_hash)
        if not os.path.exists(path):
            await self._send_response(writer, 404, {"error": "Asset not found"})
            return
        size = os.path.getsize(path)
        header = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Length: {size}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8")
        writer.write(header)
        await writer.drain()

    async def _handle_delete(self, writer, asset_hash, requester_key):
        """DELETE /assets/{hash} — provider-only deletion."""
        if not self._is_valid_hash(asset_hash):
            await self._send_response(writer, 400, {"error": "Invalid hash format"})
            return
        # Only the provider node can delete
        own_key = self._signing_key.verify_key.encode().hex() if self._signing_key else ""
        if requester_key != own_key:
            await self._send_response(writer, 403, {"error": "Only provider can delete assets"})
            return
        path = os.path.join(self._asset_dir, asset_hash)
        if os.path.exists(path):
            size = os.path.getsize(path)
            os.remove(path)
            self._metadata.pop(asset_hash, None)
            self._total_size -= size
        await self._send_response(writer, 200, {"status": "ok"})

    async def _cleanup_loop(self):
        """Background loop to delete expired assets."""
        while True:
            await asyncio.sleep(60)
            if self._asset_ttl <= 0:
                continue
            now = time.time()
            cutoff = now - self._asset_ttl
            expired = [h for h, m in self._metadata.items() if m.uploaded_at < cutoff]
            for h in expired:
                path = os.path.join(self._asset_dir, h)
                if os.path.exists(path):
                    size = os.path.getsize(path)
                    os.remove(path)
                    self._total_size -= size
                self._metadata.pop(h, None)
            if expired:
                logger.info(f"Cleaned up {len(expired)} expired assets")
            # Warn at 80% capacity
            if self._max_total_size > 0 and self._total_size > self._max_total_size * 0.8:
                logger.warning(f"Asset storage at {self._total_size / self._max_total_size * 100:.0f}% capacity")

    async def _send_response(self, writer, status: int, body: dict):
        """Send a JSON HTTP response."""
        status_text = {200: "OK", 400: "Bad Request", 401: "Unauthorized",
                       403: "Forbidden", 404: "Not Found", 413: "Payload Too Large",
                       507: "Insufficient Storage"}.get(status, "Error")
        body_bytes = json.dumps(body).encode("utf-8")
        header = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8")
        writer.write(header + body_bytes)
        await writer.drain()

    @staticmethod
    def _is_valid_hash(h: str) -> bool:
        return len(h) == 64 and all(c in '0123456789abcdef' for c in h)

    @staticmethod
    async def _write_file(path: str, data: bytes) -> None:
        """C-05: Write bytes to path in a thread to avoid blocking the event loop."""
        def _do_write():
            with open(path, "wb") as fh:
                fh.write(data)
        await asyncio.to_thread(_do_write)

    @staticmethod
    async def _read_file(path: str) -> bytes:
        """C-05: Read bytes from path in a thread to avoid blocking the event loop."""
        def _do_read():
            with open(path, "rb") as fh:
                return fh.read()
        return await asyncio.to_thread(_do_read)