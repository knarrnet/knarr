"""Tests for ordered schema migrations."""
import sqlite3
import pytest
from knarr.core.migrations import run_migrations
from knarr.dht.storage import Storage


@pytest.fixture
def migrations_dir(tmp_path):
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "v0_23_0.sql").write_text("CREATE TABLE test_a (id INTEGER);")
    (d / "v0_25_0.sql").write_text("CREATE TABLE test_b (id INTEGER);")
    (d / "v0_26_0.sql").write_text("CREATE TABLE test_c (id INTEGER);")
    return str(d)


def test_migration_runner_fresh_db(migrations_dir):
    conn = sqlite3.connect(":memory:")
    count = run_migrations(conn, migrations_dir)
    assert count == 3
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "test_a" in tables
    assert "test_b" in tables
    assert "test_c" in tables
    assert "schema_version" in tables


def test_migration_runner_idempotent(migrations_dir):
    conn = sqlite3.connect(":memory:")
    run_migrations(conn, migrations_dir)
    count = run_migrations(conn, migrations_dir)
    assert count == 0


def test_migration_version_tracking(migrations_dir):
    conn = sqlite3.connect(":memory:")
    run_migrations(conn, migrations_dir)
    applied = {r[0] for r in conn.execute(
        "SELECT version FROM schema_version"
    ).fetchall()}
    assert applied == {"v0_23_0", "v0_25_0", "v0_26_0"}


def test_storage_runs_migrations():
    """Storage._init_db should run migrations and create new tables."""
    storage = Storage(":memory:")
    # v0.26.0 migration creates mail_correspondents
    tables = {r[0] for r in storage._get_conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "mail_correspondents" in tables
    storage.close()


def test_correspondent_crud():
    """Test correspondent tracking methods."""
    storage = Storage(":memory:")
    storage.upsert_correspondent("node_A", sent=True, received=False)
    storage.upsert_correspondent("node_B", sent=False, received=True)

    import time
    time.sleep(0.01)
    storage.upsert_correspondent("node_A", sent=True, received=False)

    corrs = storage.get_correspondents(limit=10)
    assert len(corrs) == 2
    # A should be most recent
    assert corrs[0]["node_id"] == "node_A"
    assert corrs[0]["last_received"] is None
    assert corrs[0]["last_sent"] > 0
    assert corrs[1]["node_id"] == "node_B"
    assert corrs[1]["last_sent"] is None
    assert corrs[1]["last_received"] > 0
    storage.close()


def test_sentinel_migration_version_tracking(migrations_dir):
    """Sentinel: schema_version table must be consulted on every run."""
    conn = sqlite3.connect(":memory:")
    run_migrations(conn, migrations_dir)
    # Verify schema_version was created and populated
    count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert count == 3
    # Add a new migration after the fact
    import os
    with open(os.path.join(migrations_dir, "v0_27_0.sql"), "w") as f:
        f.write("CREATE TABLE test_d (id INTEGER);")
    count2 = run_migrations(conn, migrations_dir)
    assert count2 == 1
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 4
