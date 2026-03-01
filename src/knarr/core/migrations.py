"""Ordered, versioned schema migrations for knarr SQLite databases.

All migration SQL should be idempotent (CREATE IF NOT EXISTS,
ALTER TABLE ADD COLUMN that may already exist). Each statement
is executed individually so that a failing ALTER TABLE (column
already exists) does not block subsequent statements in the file.
"""
import os
import sqlite3
import logging
import time

log = logging.getLogger(__name__)


def _split_statements(sql: str) -> list:
    """Split SQL file into individual statements, ignoring comments and blanks."""
    stmts = []
    for raw in sql.split(";"):
        # Strip comments and whitespace
        lines = [l for l in raw.strip().splitlines()
                 if l.strip() and not l.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if stmt:
            stmts.append(stmt)
    return stmts


def run_migrations(conn: sqlite3.Connection, migrations_dir: str) -> int:
    """Run all pending SQL migrations in order. Returns count applied.

    Migration files: v0_23_0.sql, v0_25_0.sql, v0_26_0.sql
    Naming: v{major}_{minor}_{patch}.sql — sorted lexicographically.
    Each statement executed individually with per-statement error handling.
    """
    # 1. Create tracking table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version TEXT PRIMARY KEY,
            applied_at REAL NOT NULL
        )
    """)
    conn.commit()

    # 2. Get applied versions
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_version").fetchall()}

    # 3. Find migration files
    if not os.path.isdir(migrations_dir):
        return 0

    files = sorted(f for f in os.listdir(migrations_dir) if f.endswith('.sql'))
    count = 0

    for filename in files:
        version = filename.replace('.sql', '')
        if version in applied:
            continue

        filepath = os.path.join(migrations_dir, filename)
        with open(filepath, 'r') as f:
            sql = f.read()

        # Execute each statement individually (F-5 fix: don't abort on first error)
        stmts = _split_statements(sql)
        errors = 0
        for stmt in stmts:
            try:
                conn.execute(stmt)
            except Exception as e:
                errors += 1
                log.debug(f"Migration {version} stmt skipped: {e}")
        conn.commit()

        # Mark as applied
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, time.time())
        )
        conn.commit()
        count += 1

        if errors:
            log.warning(f"Migration {version} applied with {errors} skipped statement(s) (idempotent)")
        else:
            log.info(f"Migration applied: {version}")

    return count
