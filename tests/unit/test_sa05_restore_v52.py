"""SA-05: Restore utility.

Tests verify:
- restore_archive creates a temp table with archived rows
- Restored rows match the original archived data
- Unencrypted archive restores correctly
- Missing/invalid archive raises ValueError
- Temp table name is returned
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

from archive import archive_table, restore_archive


def make_source_conn():
    """Create SQLite with receipt_log and some rows."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE receipt_log (
            receipt_id TEXT PRIMARY KEY, document_type TEXT,
            created_at REAL NOT NULL, payload_json TEXT
        )
    """)
    now = time.time()
    conn.execute("INSERT INTO receipt_log VALUES ('r1', 'receipt', ?, '{}')", (now - 200,))
    conn.execute("INSERT INTO receipt_log VALUES ('r2', 'receipt', ?, '{}')", (now - 100,))
    conn.commit()
    return conn


# ──────────────────────────────────────────────────────────────────────────────
# SA-05-A: restore_archive returns a temp table name
# ──────────────────────────────────────────────────────────────────────────────

def test_restore_returns_temp_table_name():
    """restore_archive must return a string (temp table name)."""
    source_conn = make_source_conn()
    restore_conn = sqlite3.connect(":memory:")

    with tempfile.TemporaryDirectory() as tmpdir:
        archive_table(source_conn, "receipt_log", time.time() + 1, tmpdir, compress=False)

        archive_dir = os.path.join(tmpdir, "receipt_log")
        archive_files = [f for f in os.listdir(archive_dir) if f.endswith(".jsonl")]
        assert len(archive_files) == 1
        archive_path = os.path.join(archive_dir, archive_files[0])

        temp_name = restore_archive(archive_path, "receipt_log", restore_conn)

    assert isinstance(temp_name, str)
    assert "receipt_log" in temp_name


# ──────────────────────────────────────────────────────────────────────────────
# SA-05-B: Restored rows match original data
# ──────────────────────────────────────────────────────────────────────────────

def test_restore_rows_match_original():
    """Rows in the temp table must match the original archived rows."""
    source_conn = make_source_conn()
    restore_conn = sqlite3.connect(":memory:")

    with tempfile.TemporaryDirectory() as tmpdir:
        count = archive_table(source_conn, "receipt_log", time.time() + 1, tmpdir, compress=False)
        assert count == 2

        archive_dir = os.path.join(tmpdir, "receipt_log")
        archive_files = [f for f in os.listdir(archive_dir) if f.endswith(".jsonl")]
        archive_path = os.path.join(archive_dir, archive_files[0])

        temp_name = restore_archive(archive_path, "receipt_log", restore_conn)

    rows = restore_conn.execute(f"SELECT receipt_id FROM {temp_name}").fetchall()
    receipt_ids = {row[0] for row in rows}
    assert "r1" in receipt_ids
    assert "r2" in receipt_ids


# ──────────────────────────────────────────────────────────────────────────────
# SA-05-C: Compressed archive restores correctly
# ──────────────────────────────────────────────────────────────────────────────

def test_restore_compressed_archive():
    """Gzipped archives must restore correctly."""
    source_conn = make_source_conn()
    restore_conn = sqlite3.connect(":memory:")

    with tempfile.TemporaryDirectory() as tmpdir:
        archive_table(source_conn, "receipt_log", time.time() + 1, tmpdir, compress=True)

        archive_dir = os.path.join(tmpdir, "receipt_log")
        gz_files = [f for f in os.listdir(archive_dir) if f.endswith(".jsonl.gz")]
        assert len(gz_files) == 1
        archive_path = os.path.join(archive_dir, gz_files[0])

        temp_name = restore_archive(archive_path, "receipt_log", restore_conn)

    rows = restore_conn.execute(f"SELECT receipt_id FROM {temp_name}").fetchall()
    assert len(rows) == 2


# ──────────────────────────────────────────────────────────────────────────────
# SA-05-D: Non-existent archive raises exception
# ──────────────────────────────────────────────────────────────────────────────

def test_restore_nonexistent_archive_raises():
    """restore_archive must raise when the archive file does not exist."""
    restore_conn = sqlite3.connect(":memory:")
    with pytest.raises(Exception):
        restore_archive("/nonexistent/path/archive.jsonl", "receipt_log", restore_conn)
