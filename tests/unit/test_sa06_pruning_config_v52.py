"""SA-06: Configurable pruning hooks.

Tests verify:
- PruningConfig reads retention periods from config dict
- PruningConfig provides correct cutoff timestamps
- TablePruner.prune_all deletes rows older than configured retention
- Thrall tables are NOT managed by this pruner
- Zero retention period = skip that table
"""

import sqlite3
import sys
import os
import time
import pytest

_plugin_dir = os.path.join(
    os.path.dirname(__file__), "..", "..", "plugins", "00-storage-strategy"
)
if _plugin_dir not in sys.path:
    sys.path.insert(0, os.path.abspath(_plugin_dir))

from pruning import PruningConfig, TablePruner, DEFAULT_RETENTION


# ──────────────────────────────────────────────────────────────────────────────
# SA-06-A: PruningConfig defaults
# ──────────────────────────────────────────────────────────────────────────────

def test_pruning_config_defaults():
    """PruningConfig with no overrides must use DEFAULT_RETENTION values."""
    config = PruningConfig()
    for table, default_ttl in DEFAULT_RETENTION.items():
        assert config.retention_seconds(table) == default_ttl


# ──────────────────────────────────────────────────────────────────────────────
# SA-06-B: PruningConfig respects overrides
# ──────────────────────────────────────────────────────────────────────────────

def test_pruning_config_overrides():
    """PruningConfig must use provided values instead of defaults."""
    config = PruningConfig({"receipt_log": 3600, "async_jobs": 7200})
    assert config.retention_seconds("receipt_log") == 3600
    assert config.retention_seconds("async_jobs") == 7200
    # Unspecified tables fall back to defaults
    assert config.retention_seconds("execution_log") == DEFAULT_RETENTION["execution_log"]


# ──────────────────────────────────────────────────────────────────────────────
# SA-06-C: should_prune returns False for zero retention
# ──────────────────────────────────────────────────────────────────────────────

def test_pruning_config_zero_retention():
    """Zero retention period must disable pruning for that table."""
    config = PruningConfig({"receipt_log": 0})
    assert config.should_prune("receipt_log") is False
    assert config.retention_seconds("receipt_log") == 0


# ──────────────────────────────────────────────────────────────────────────────
# SA-06-D: cutoff_ts returns correct timestamp
# ──────────────────────────────────────────────────────────────────────────────

def test_pruning_config_cutoff_ts():
    """cutoff_ts must return approximately now - retention_seconds."""
    config = PruningConfig({"receipt_log": 3600})
    before = time.time()
    cutoff = config.cutoff_ts("receipt_log")
    after = time.time()

    assert (before - 3600) <= cutoff <= (after - 3600)


# ──────────────────────────────────────────────────────────────────────────────
# SA-06-E: TablePruner.prune_all deletes old rows
# ──────────────────────────────────────────────────────────────────────────────

def test_pruner_deletes_old_rows():
    """TablePruner must delete rows older than configured retention."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE receipt_log (
            receipt_id TEXT PRIMARY KEY, created_at REAL NOT NULL
        )
    """)
    now = time.time()
    conn.execute("INSERT INTO receipt_log VALUES ('old', ?)", (now - 10000,))
    conn.execute("INSERT INTO receipt_log VALUES ('new', ?)", (now - 10,))
    conn.commit()

    # Use a mock storage that exposes _get_conn
    class MockStorage:
        def _get_conn(self):
            return conn

    config = PruningConfig({"receipt_log": 3600})  # 1 hour retention
    # TP-4: Must pass archived_tables so pruner knows archive succeeded for this table
    pruner = TablePruner(MockStorage(), config, archived_tables={"receipt_log"})
    results = pruner.prune_all()

    remaining = conn.execute("SELECT receipt_id FROM receipt_log").fetchall()
    assert len(remaining) == 1
    assert remaining[0][0] == "new"


# ──────────────────────────────────────────────────────────────────────────────
# SA-06-F: Thrall tables are not in pruner config
# ──────────────────────────────────────────────────────────────────────────────

def test_thrall_tables_not_in_pruner():
    """Thrall tables must NOT be in DEFAULT_RETENTION (managed by thrall plugin)."""
    thrall_tables = {"thrall_journal", "thrall_memory"}
    assert not thrall_tables.intersection(DEFAULT_RETENTION.keys()), (
        f"Thrall tables found in DEFAULT_RETENTION: {thrall_tables & DEFAULT_RETENTION.keys()}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# SA-06-G: PruningConfig.all_tables returns dict
# ──────────────────────────────────────────────────────────────────────────────

def test_pruning_config_all_tables():
    """PruningConfig.all_tables must return dict with at least the default tables."""
    config = PruningConfig()
    tables = config.all_tables()
    assert isinstance(tables, dict)
    for table in DEFAULT_RETENTION:
        assert table in tables
