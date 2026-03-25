"""SA-03: Archive rotation.

Tests verify:
- archive_table selects rows older than cutoff and writes to archive
- Archived rows are deleted from the hot table
- archive_table returns count of archived rows
- Empty table returns 0
- Archive file is valid JSONL (optionally gzipped)
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


def make_conn_with_table(rows=None):
    """Create an in-memory SQLite with receipt_log and optional test rows."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE receipt_log (
            receipt_id TEXT PRIMARY KEY,
            document_type TEXT,
            created_at REAL NOT NULL,
            payload_json TEXT
        )
    """)
    if rows:
        for row in rows:
            conn.execute(
                "INSERT INTO receipt_log VALUES (?, ?, ?, ?)",
                (row["receipt_id"], row["document_type"], row["created_at"], row.get("payload_json", "{}"))
            )
    conn.commit()
    return conn


# ──────────────────────────────────────────────────────────────────────────────
# SA-03-A: archive_table returns correct count
# ──────────────────────────────────────────────────────────────────────────────

def test_archive_table_returns_count():
    """archive_table must return the number of rows archived."""
    conn = make_conn_with_table(rows=[
        {"receipt_id": "r1", "document_type": "receipt", "created_at": time.time() - 1000},
        {"receipt_id": "r2", "document_type": "receipt", "created_at": time.time() - 500},
        {"receipt_id": "r3", "document_type": "receipt", "created_at": time.time() + 100},  # future — not archived
    ])

    with tempfile.TemporaryDirectory() as tmpdir:
        count = archive_table(conn, "receipt_log", time.time(), tmpdir, compress=False)
    assert count == 2


# ──────────────────────────────────────────────────────────────────────────────
# SA-03-B: Archived rows deleted from hot table
# ──────────────────────────────────────────────────────────────────────────────

def test_archive_deletes_rows():
    """Rows older than cutoff must be deleted from the hot table after archiving."""
    conn = make_conn_with_table(rows=[
        {"receipt_id": "r1", "document_type": "x", "created_at": time.time() - 1000},
        {"receipt_id": "r2", "document_type": "x", "created_at": time.time() + 100},
    ])

    with tempfile.TemporaryDirectory() as tmpdir:
        archive_table(conn, "receipt_log", time.time(), tmpdir, compress=False)

    remaining = conn.execute("SELECT receipt_id FROM receipt_log").fetchall()
    assert len(remaining) == 1
    assert remaining[0][0] == "r2"


# ──────────────────────────────────────────────────────────────────────────────
# SA-03-C: Empty table returns 0
# ──────────────────────────────────────────────────────────────────────────────

def test_archive_empty_table():
    """archive_table must return 0 when there are no rows to archive."""
    conn = make_conn_with_table(rows=[])
    with tempfile.TemporaryDirectory() as tmpdir:
        count = archive_table(conn, "receipt_log", time.time(), tmpdir)
    assert count == 0


# ──────────────────────────────────────────────────────────────────────────────
# SA-03-D: Archive file is valid JSONL (uncompressed)
# ──────────────────────────────────────────────────────────────────────────────

def test_archive_file_is_valid_jsonl():
    """Uncompressed archive file must contain valid JSONL."""
    conn = make_conn_with_table(rows=[
        {"receipt_id": "r1", "document_type": "receipt", "created_at": time.time() - 100},
    ])

    with tempfile.TemporaryDirectory() as tmpdir:
        count = archive_table(conn, "receipt_log", time.time(), tmpdir, compress=False)
        assert count == 1

        # Find the archive file
        archive_dir = os.path.join(tmpdir, "receipt_log")
        files = [f for f in os.listdir(archive_dir) if f.endswith(".jsonl")]
        assert len(files) == 1

        with open(os.path.join(archive_dir, files[0])) as fh:
            line = fh.readline().strip()
        record = json.loads(line)
        assert record["receipt_id"] == "r1"


# ──────────────────────────────────────────────────────────────────────────────
# SA-03-E: Archive file is valid gzipped JSONL
# ──────────────────────────────────────────────────────────────────────────────

def test_archive_file_is_gzipped():
    """Compressed archive must produce a .jsonl.gz file with valid content."""
    conn = make_conn_with_table(rows=[
        {"receipt_id": "r1", "document_type": "receipt", "created_at": time.time() - 100},
    ])

    with tempfile.TemporaryDirectory() as tmpdir:
        count = archive_table(conn, "receipt_log", time.time(), tmpdir, compress=True)
        assert count == 1

        archive_dir = os.path.join(tmpdir, "receipt_log")
        gz_files = [f for f in os.listdir(archive_dir) if f.endswith(".jsonl.gz")]
        assert len(gz_files) == 1

        with gzip.open(os.path.join(archive_dir, gz_files[0]), "rt") as fh:
            line = fh.readline().strip()
        record = json.loads(line)
        assert record["receipt_id"] == "r1"
