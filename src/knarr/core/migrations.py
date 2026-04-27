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
    """Split SQL file into individual statements, ignoring comments and blanks.

    B-01 (v0.58.0): State machine that correctly handles:
    - Single-quoted strings with '' escape (e.g. 'it''s;')
    - Double-quoted identifiers (e.g. "col;name")
    - ``--`` line comments (to end of line)
    - ``/* ... */`` block comments (may span multiple lines)

    ``;`` inside any of the above contexts does NOT terminate a statement.
    Unterminated strings or block comments raise ValueError.

    Backticks and ``$$`` dollar-quoted strings are out of scope and documented
    as unsupported; they will be treated as literal characters.
    """
    stmts: list[str] = []
    current: list[str] = []
    i = 0
    n = len(sql)

    while i < n:
        ch = sql[i]

        # Single-quoted string: '...' with '' escape
        if ch == "'":
            current.append(ch)
            i += 1
            found_close = False
            while i < n:
                ch2 = sql[i]
                current.append(ch2)
                if ch2 == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        # '' escape: consume the second quote too
                        i += 1
                        if i < n:
                            current.append(sql[i])
                        i += 1
                        continue
                    else:
                        found_close = True
                        break
                i += 1
            if not found_close:
                raise ValueError("Unterminated single-quoted string in migration SQL")
            i += 1
            continue

        # Double-quoted identifier: "..."
        if ch == '"':
            current.append(ch)
            i += 1
            while i < n:
                ch2 = sql[i]
                current.append(ch2)
                if ch2 == '"':
                    i += 1
                    break  # end of identifier
                i += 1
            else:
                raise ValueError("Unterminated double-quoted identifier in migration SQL")
            continue

        # Line comment: -- to end of line
        if ch == '-' and i + 1 < n and sql[i + 1] == '-':
            # Skip to end of line
            i += 2
            while i < n and sql[i] != '\n':
                i += 1
            continue

        # Block comment: /* ... */
        if ch == '/' and i + 1 < n and sql[i + 1] == '*':
            i += 2
            while i + 1 < n:
                if sql[i] == '*' and sql[i + 1] == '/':
                    i += 2
                    break
                i += 1
            else:
                raise ValueError("Unterminated block comment in migration SQL")
            continue

        # Semicolon: statement terminator
        if ch == ';':
            stmt = ''.join(current).strip()
            if stmt:
                stmts.append(stmt)
            current.clear()
            i += 1
            continue

        # Regular character
        current.append(ch)
        i += 1

    # Handle last statement (no trailing semicolon)
    stmt = ''.join(current).strip()
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
        real_errors = 0
        for stmt in stmts:
            try:
                conn.execute(stmt)
            except Exception as e:
                msg = str(e).lower()
                if "already exists" in msg or "duplicate column" in msg:
                    log.debug(f"Migration {version} stmt skipped (idempotent): {e}")
                else:
                    real_errors += 1
                    log.warning(f"Migration {version} stmt FAILED: {e}")
        conn.commit()

        if real_errors > 0:
            log.error(
                f"Migration {version} NOT marked applied — "
                f"{real_errors} non-idempotent error(s); will retry on next startup"
            )
            continue

        # Mark as applied only when all non-idempotent statements succeeded
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, time.time())
        )
        conn.commit()
        count += 1
        log.info(f"Migration applied: {version}")

    return count
