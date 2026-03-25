"""SA-06: Configurable pruning hooks for core database tables.

Per-table retention periods are configured in plugin.toml [config.retention].
Tables are either archived before deletion (receipt_log, execution_log) or
directly pruned (async_jobs, settlement_queue, and others).

NOTE: Thrall tables (thrall_journal, thrall_memory) are managed by the thrall
plugin in a separate DB and are NOT handled here.
"""

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Default retention in seconds (0 = never prune)
DEFAULT_RETENTION: Dict[str, int] = {
    "receipt_log": 604800,        # 7 days — archived
    "execution_log": 604800,      # 7 days — archived
    "async_jobs": 86400,          # 1 day — deleted
    "settlement_queue": 259200,   # 3 days — deleted
}

# Tables that are archived before deletion vs directly deleted
ARCHIVE_TABLES = {"receipt_log", "execution_log"}
PRUNE_ONLY_TABLES = {"async_jobs", "settlement_queue"}

# Additional prunable tables that just need direct deletion (no archive)
EXTRA_PRUNE_TABLES = {"mail_outbox", "mail_inbox", "mail_jobreport", "mail_system", "mail_creditnote"}

# TP-6: Allowlist for table names used in SQL — prevents injection via f-strings
_ALLOWED_TABLES = ARCHIVE_TABLES | PRUNE_ONLY_TABLES | EXTRA_PRUNE_TABLES

# SA-06: Status filters for pruning — only prune rows in terminal states.
# Critical: pruning settlement_queue without filtering would delete pending settlements.
STATUS_FILTERS: Dict[str, str] = {
    "async_jobs": "status IN ('completed', 'failed', 'expired')",
    "settlement_queue": "status = 'processed'",
}


class PruningConfig:
    """Parsed retention configuration from plugin.toml."""

    def __init__(self, retention_config: Optional[Dict[str, Any]] = None):
        config = retention_config or {}
        self._retention: Dict[str, int] = {}
        for table, default_ttl in DEFAULT_RETENTION.items():
            raw = config.get(table, default_ttl)
            try:
                self._retention[table] = max(0, int(raw))
            except (TypeError, ValueError):
                logger.warning("PRUNING_CONFIG_INVALID table=%s value=%r using_default=%d", table, raw, default_ttl)
                self._retention[table] = default_ttl

    def retention_seconds(self, table: str) -> int:
        """Return retention period in seconds for a table (0 = never prune)."""
        return self._retention.get(table, 0)

    def should_prune(self, table: str) -> bool:
        """Return True if the table has a non-zero retention period configured."""
        return self.retention_seconds(table) > 0

    def cutoff_ts(self, table: str) -> float:
        """Return the UNIX timestamp before which rows should be pruned."""
        return time.time() - self.retention_seconds(table)

    def all_tables(self) -> Dict[str, int]:
        """Return all configured retention periods."""
        return dict(self._retention)


class TablePruner:
    """Executes pruning operations against the storage layer.

    Uses direct SQL for tables that have pruning support in storage,
    and falls back to _get_conn() only when no storage method is available.
    """

    def __init__(self, storage, pruning_config: PruningConfig, archive_fn=None,
                 archived_tables: set = None):
        """
        Args:
            storage: Storage instance (or StorageCacheProxy wrapping one)
            pruning_config: PruningConfig instance with retention periods
            archive_fn: Optional callable(table, rows) for archiving before deletion
            archived_tables: TP-4: Set of tables successfully archived this cycle.
                           Archive-eligible tables not in this set will be skipped.
        """
        self._storage = storage
        self._config = pruning_config
        self._archive_fn = archive_fn
        self._archived_tables = archived_tables or set()

    def prune_all(self) -> Dict[str, int]:
        """Run all configured pruning operations.

        Returns a dict of table -> rows pruned.
        """
        results: Dict[str, int] = {}
        now = time.time()

        for table, retention in self._config.all_tables().items():
            if retention == 0:
                continue
            # TP-4: Skip archive-eligible tables that weren't successfully archived
            if table in ARCHIVE_TABLES and table not in self._archived_tables:
                logger.info("PRUNER_SKIP table=%s reason=archive_not_confirmed", table)
                continue
            cutoff = now - retention
            try:
                deleted = self._prune_table(table, cutoff, retention)
                if deleted:
                    results[table] = deleted
                    logger.info("PRUNER_TABLE_PRUNED table=%s retention=%ds deleted=%d", table, retention, deleted)
            except Exception as exc:
                logger.warning("PRUNER_TABLE_FAIL table=%s error=%s", table, exc)

        return results

    def _prune_table(self, table: str, cutoff: float, retention: int = 0) -> int:
        """Prune a single table up to the cutoff timestamp.

        For archive tables: calls archive_fn before deletion (if set).

        TP-3: retention (seconds) passed separately for methods that expect max_age_seconds.
        """
        # TP-6: Validate table name against allowlist
        if table not in _ALLOWED_TABLES:
            raise ValueError(f"Invalid table name: {table}")

        if table in ARCHIVE_TABLES and self._archive_fn:
            # Archive before pruning — archive_fn returns count of rows archived
            try:
                self._archive_fn(table, cutoff)
            except Exception as exc:
                logger.warning("PRUNER_ARCHIVE_FAIL table=%s error=%s", table, exc)

        # Use named storage methods when available
        # TP-3: Pass retention (seconds), not cutoff (timestamp) — these methods
        # expect max_age_seconds and do time.time() - max_age_seconds internally.
        if table == "receipt_log":
            fn = getattr(self._storage, "purge_receipt_log_by_age", None)
            if fn:
                return fn(retention) or 0

        if table == "execution_log":
            fn = getattr(self._storage, "purge_execution_log_by_age", None)
            if fn:
                return fn(retention) or 0

        if table == "settlement_queue":
            fn = getattr(self._storage, "purge_settled_queue", None)
            if fn:
                max_age = time.time() - cutoff
                return fn(max_age) or 0

        if table == "async_jobs":
            fn = getattr(self._storage, "purge_expired_async_jobs", None)
            if fn:
                return fn(cutoff) or 0

        # Fallback: direct SQL delete via the underlying storage connection
        # Only used for tables without a dedicated storage method
        raw_storage = getattr(self._storage, "_storage", self._storage)
        conn = raw_storage._get_conn()
        try:
            # SA-06: Apply status filter to only prune terminal-state rows
            status_filter = STATUS_FILTERS.get(table)
            for ts_col in ("created_at", "updated_at", "timestamp"):
                try:
                    where = f"{ts_col} < ?"
                    params = [cutoff]
                    if status_filter:
                        where = f"{status_filter} AND {where}"
                    cursor = conn.execute(
                        f"DELETE FROM {table} WHERE {where}", params
                    )
                    conn.commit()
                    return cursor.rowcount
                except Exception as col_exc:
                    # TP-10: Log warning instead of silently swallowing column mismatch errors
                    logger.warning("PRUNER_COL_FAIL table=%s col=%s error=%s", table, ts_col, col_exc)
                    continue
        except Exception as exc:
            logger.debug("PRUNER_DIRECT_FAIL table=%s error=%s", table, exc)
        return 0
