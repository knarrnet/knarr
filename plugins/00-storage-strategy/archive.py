"""SA-03/SA-04/SA-05: Archive rotation with optional signed encryption.

archive_table(conn, table, cutoff_ts, archive_dir, compress=True, vault=None, sign_fn=None):
  - SELECT rows older than cutoff_ts
  - Write to {archive_dir}/{table}/{YYYY-MM-DD}.jsonl[.gz]
  - Optionally encrypt (nacl SecretBox) and sign
  - DELETE archived rows from hot table

restore_archive(archive_path, table_name, conn, vault=None, verify_fn=None) -> temp_table_name:
  - Verify signature (if encrypted)
  - Decrypt (if encrypted)
  - Decompress (if gzipped)
  - CREATE TEMP TABLE
  - INSERT rows
  - Return temp table name
"""

import gzip
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Column used to select rows for archiving (table-specific overrides below)
_TIMESTAMP_COLS: Dict[str, str] = {
    "receipt_log": "created_at",
    "execution_log": "created_at",
    "async_jobs": "created_at",
    "settlement_queue": "created_at",
}


# TP-6: Allowlist for table names used in SQL — prevents injection via f-strings
_ALLOWED_TABLES = set(_TIMESTAMP_COLS.keys())


def _get_timestamp_col(table: str) -> str:
    return _TIMESTAMP_COLS.get(table, "created_at")


def archive_table(
    conn: sqlite3.Connection,
    table: str,
    cutoff_ts: float,
    archive_dir: str,
    compress: bool = True,
    vault=None,
    sign_fn=None,
    delete_fn=None,
) -> int:
    """Archive rows older than cutoff_ts from table.

    SA-03: Basic archive — SELECT → write JSONL → DELETE.
    SA-04: When vault + sign_fn provided — encrypt then sign the archive.

    Args:
        delete_fn: Optional callable(table, ts_col, cutoff_ts) for the DELETE.
                   When provided, the DELETE is delegated to this function
                   (e.g., to route through _enqueue_write). When None, a direct
                   SQL DELETE + commit is used.

    Returns count of rows archived.
    """
    # TP-6: Validate table name against allowlist before SQL interpolation
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"Invalid table name: {table}")

    ts_col = _get_timestamp_col(table)

    # Fetch rows to archive
    try:
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE {ts_col} < ?",
            (cutoff_ts,)
        ).fetchall()
    except Exception as exc:
        logger.warning("ARCHIVE_SELECT_FAIL table=%s error=%s", table, exc)
        return 0

    if not rows:
        return 0

    # Get column names
    cols = [description[0] for description in conn.execute(
        f"SELECT * FROM {table} LIMIT 0"
    ).description]

    # Build JSONL content
    lines = []
    for row in rows:
        record = dict(zip(cols, row))
        lines.append(json.dumps(record, separators=(",", ":")))
    content_bytes = "\n".join(lines).encode("utf-8")

    # Build archive path
    date_str = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    table_dir = os.path.join(archive_dir, table)
    os.makedirs(table_dir, exist_ok=True)

    if compress:
        raw = gzip.compress(content_bytes, compresslevel=6)
        ext = ".jsonl.gz"
    else:
        raw = content_bytes
        ext = ".jsonl"

    # SA-04: Encrypt then sign if vault and sign_fn provided
    metadata: Dict[str, Any] = {
        "table": table,
        "rows": len(rows),
        "cutoff_ts": cutoff_ts,
        "created_at": time.time(),
        "compressed": compress,
        "encrypted": False,
    }

    if vault is not None and sign_fn is not None:
        try:
            # Encrypt with SecretBox
            raw, metadata = _encrypt_and_sign(raw, metadata, vault, sign_fn)
            ext = ext + ".enc"
        except Exception as exc:
            logger.warning("ARCHIVE_ENCRYPT_FAIL table=%s error=%s — writing unencrypted", table, exc)

    # Include row count in filename to avoid collisions within same date
    ts_suffix = int(time.time())
    filename = f"{date_str}_{ts_suffix}{ext}"
    archive_path = os.path.join(table_dir, filename)

    # Write archive + metadata sidecar
    with open(archive_path, "wb") as fh:
        fh.write(raw)

    meta_path = archive_path + ".meta.json"
    with open(meta_path, "w") as fh:
        json.dump(metadata, fh, indent=2)

    # SA-03: Delete archived rows — use delete_fn if provided (_enqueue_write path)
    try:
        if delete_fn is not None:
            delete_fn(table, ts_col, cutoff_ts)
        else:
            conn.execute(
                f"DELETE FROM {table} WHERE {ts_col} < ?",
                (cutoff_ts,)
            )
            conn.commit()
    except Exception as exc:
        logger.error("ARCHIVE_DELETE_FAIL table=%s error=%s — archive written but rows NOT deleted", table, exc)
        return 0

    logger.info("ARCHIVE_COMPLETE table=%s rows=%d path=%s", table, len(rows), archive_path)
    return len(rows)


def _encrypt_and_sign(
    data: bytes,
    metadata: Dict[str, Any],
    vault,
    sign_fn,
) -> tuple:
    """SA-04: compress → encrypt → sign.

    Uses vault.encrypt_bytes() (public API) for encryption.
    Uses sign_fn(bytes) -> (signed_bytes, sig_hex) for signing.

    Returns (encrypted_signed_bytes, updated_metadata).
    """
    # SA-04: Encrypt using vault's public encrypt_bytes API
    encrypted = vault.encrypt_bytes(data)

    # Sign the encrypted blob — store signature in metadata, return ciphertext
    # TP-2: Return encrypted payload, NOT the signature bytes
    signed_bytes, sig_hex = sign_fn(encrypted)

    metadata["encrypted"] = True
    metadata["signature"] = sig_hex

    return encrypted, metadata


def restore_archive(
    archive_path: str,
    table_name: str,
    conn: sqlite3.Connection,
    vault=None,
    verify_fn=None,
) -> str:
    """SA-05: Restore an archive into a temporary SQLite table.

    Steps: verify sig → decrypt → decompress → CREATE TEMP TABLE → INSERT → return name.

    Returns the temporary table name (prefixed with 'temp_restore_').
    """
    # TP-6: Validate table name against allowlist before SQL interpolation
    if table_name not in _ALLOWED_TABLES:
        raise ValueError(f"Invalid table name: {table_name}")

    meta_path = archive_path + ".meta.json"
    metadata: Dict[str, Any] = {}
    if os.path.exists(meta_path):
        with open(meta_path, "r") as fh:
            metadata = json.load(fh)

    with open(archive_path, "rb") as fh:
        raw = fh.read()

    is_encrypted = metadata.get("encrypted", False)
    is_compressed = metadata.get("compressed", False)

    # SA-05: Verify then decrypt if encrypted
    if is_encrypted:
        if verify_fn is None or vault is None:
            raise ValueError(f"Archive {archive_path} is encrypted but no vault/verify_fn provided")

        sig_hex = metadata.get("signature", "")
        if not verify_fn(raw, sig_hex):
            raise ValueError(f"Archive signature verification failed for {archive_path}")

        # SA-04: Decrypt using vault's public decrypt_bytes API
        raw = vault.decrypt_bytes(raw)

    # Decompress
    if is_compressed:
        raw = gzip.decompress(raw)

    # Parse JSONL
    records: List[Dict] = []
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            logger.warning("RESTORE_JSON_FAIL path=%s error=%s", archive_path, exc)

    if not records:
        raise ValueError(f"Archive {archive_path} contains no valid rows")

    # SA-05: Preserve column types via PRAGMA table_info() (B's approach).
    # When the table exists in the target DB, use its schema for type-accurate
    # temp table creation. When it doesn't exist (e.g. restoring to a fresh DB),
    # fall back to record keys with TEXT columns.
    schema_rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    if schema_rows:
        columns = [row[1] for row in schema_rows]
        col_defs = ", ".join(f"{row[1]} {row[2] or 'TEXT'}" for row in schema_rows)
    else:
        columns = list(records[0].keys())
        col_defs = ", ".join(f"{c} TEXT" for c in columns)

    temp_name = f"temp_restore_{table_name}_{int(time.time())}"

    try:
        conn.execute(f"CREATE TEMP TABLE {temp_name} ({col_defs})")
        placeholders = ", ".join("?" for _ in columns)
        conn.executemany(
            f"INSERT INTO {temp_name} ({', '.join(columns)}) VALUES ({placeholders})",
            [[record.get(c) for c in columns] for record in records],
        )
        conn.commit()
    except Exception as exc:
        logger.error("RESTORE_INSERT_FAIL table=%s error=%s", temp_name, exc)
        raise

    logger.info("RESTORE_COMPLETE path=%s table=%s rows=%d", archive_path, temp_name, len(records))
    return temp_name
