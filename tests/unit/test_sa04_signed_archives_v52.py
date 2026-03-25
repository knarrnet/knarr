"""SA-04: Signed encrypted archives.

Tests verify:
- archive_table with vault+sign_fn produces encrypted archive (.enc extension)
- Metadata records encrypted=True and signature
- Encrypted content differs from plaintext
- Signature is non-empty
"""

import gzip
import json
import os
import sqlite3
import sys
import tempfile
import time
import pytest

_plugin_dir = os.path.join(
    os.path.dirname(__file__), "..", "..", "plugins", "00-storage-strategy"
)
if _plugin_dir not in sys.path:
    sys.path.insert(0, os.path.abspath(_plugin_dir))

from archive import archive_table, _encrypt_and_sign


def make_conn_with_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE receipt_log (
            receipt_id TEXT PRIMARY KEY, document_type TEXT,
            created_at REAL NOT NULL, payload_json TEXT
        )
    """)
    conn.execute(
        "INSERT INTO receipt_log VALUES ('r1', 'receipt', ?, '{}')",
        (time.time() - 100,)
    )
    conn.commit()
    return conn


class MockVault:
    """Minimal vault mock using nacl SecretBox — SA-04 uses public API."""

    def __init__(self):
        from nacl.secret import SecretBox
        from nacl.utils import random
        self._box = SecretBox(random(SecretBox.KEY_SIZE))

    def encrypt_bytes(self, data: bytes) -> bytes:
        return bytes(self._box.encrypt(data))

    def decrypt_bytes(self, data: bytes) -> bytes:
        return bytes(self._box.decrypt(data))


def make_sign_fn():
    """Return a simple sign_fn that uses nacl signing."""
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    vk = sk.verify_key

    def sign_fn(data: bytes):
        signed = sk.sign(data)
        return signed, signed.signature.hex()

    return sign_fn, vk


# ──────────────────────────────────────────────────────────────────────────────
# SA-04-A: archive_table with vault produces .enc file
# ──────────────────────────────────────────────────────────────────────────────

def test_archive_with_vault_produces_enc_file():
    """archive_table with vault+sign_fn must produce a .enc archive file."""
    conn = make_conn_with_rows()
    vault = MockVault()
    sign_fn, _ = make_sign_fn()

    with tempfile.TemporaryDirectory() as tmpdir:
        count = archive_table(conn, "receipt_log", time.time(), tmpdir, compress=True, vault=vault, sign_fn=sign_fn)
        assert count == 1

        archive_dir = os.path.join(tmpdir, "receipt_log")
        files = os.listdir(archive_dir)
        enc_files = [f for f in files if f.endswith(".enc")]
        assert len(enc_files) == 1, f"Expected .enc file, found: {files}"


# ──────────────────────────────────────────────────────────────────────────────
# SA-04-B: metadata records encrypted=True and signature
# ──────────────────────────────────────────────────────────────────────────────

def test_encrypted_archive_metadata():
    """Archive metadata must record encrypted=True and a non-empty signature."""
    conn = make_conn_with_rows()
    vault = MockVault()
    sign_fn, _ = make_sign_fn()

    with tempfile.TemporaryDirectory() as tmpdir:
        archive_table(conn, "receipt_log", time.time(), tmpdir, compress=True, vault=vault, sign_fn=sign_fn)

        archive_dir = os.path.join(tmpdir, "receipt_log")
        meta_files = [f for f in os.listdir(archive_dir) if f.endswith(".meta.json")]
        assert len(meta_files) == 1

        with open(os.path.join(archive_dir, meta_files[0])) as fh:
            metadata = json.load(fh)

    assert metadata["encrypted"] is True
    assert "signature" in metadata
    assert len(metadata["signature"]) > 0


# ──────────────────────────────────────────────────────────────────────────────
# SA-04-C: Encrypted content is not readable as plaintext JSONL
# ──────────────────────────────────────────────────────────────────────────────

def test_encrypted_content_not_plaintext():
    """Encrypted archive bytes must not be readable as plaintext JSONL."""
    conn = make_conn_with_rows()
    vault = MockVault()
    sign_fn, _ = make_sign_fn()

    with tempfile.TemporaryDirectory() as tmpdir:
        archive_table(conn, "receipt_log", time.time(), tmpdir, compress=True, vault=vault, sign_fn=sign_fn)

        archive_dir = os.path.join(tmpdir, "receipt_log")
        enc_files = [f for f in os.listdir(archive_dir) if f.endswith(".enc")]
        with open(os.path.join(archive_dir, enc_files[0]), "rb") as fh:
            raw = fh.read()

    # Should NOT be parseable as JSON
    try:
        json.loads(raw.decode("utf-8", errors="ignore"))
        # If we get here, the content is readable as JSON — that's wrong
        assert False, "Encrypted content should not be readable as JSON"
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass  # Expected — content is encrypted


# ──────────────────────────────────────────────────────────────────────────────
# SA-04-D: _encrypt_and_sign round-trip
# ──────────────────────────────────────────────────────────────────────────────

def test_encrypt_and_sign_produces_metadata():
    """_encrypt_and_sign must set encrypted=True and a non-empty signature in metadata."""
    vault = MockVault()
    sign_fn, verify_key = make_sign_fn()

    original_data = b"test data for encryption"
    metadata = {"encrypted": False, "table": "test"}

    signed_bytes, updated_meta = _encrypt_and_sign(original_data, metadata, vault, sign_fn)

    assert updated_meta["encrypted"] is True
    assert len(updated_meta["signature"]) > 0
    # The signed bytes should be non-empty and differ from input
    assert signed_bytes != original_data
    assert len(signed_bytes) > 0
