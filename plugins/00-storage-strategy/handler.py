"""SA-01/SA-02/SA-03/SA-04/SA-05/SA-06: Storage strategy plugin.

Wraps node.storage with:
- SA-01: TTL-based in-memory cache (StorageCacheProxy)
- SA-02: Thread-offloaded async reads (AsyncStorageMixin)
- SA-03: Archive rotation on_tick
- SA-04: Signed encrypted archives
- SA-05: Restore utility (available via ctx)
- SA-06: Configurable pruning hooks

This plugin MUST load before all others (numbered 00-).
"""

import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Ensure the plugin's own directory is on sys.path for sibling imports
_plugin_dir = os.path.dirname(__file__)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from knarr.dht.plugins import PluginHooks, PluginContext, NodeHealth
from knarr.core.messages import Message
from knarr.core.models import NodeInfo

# Plugin-local imports (relative)
from cache import StorageCacheProxy
from pruning import PruningConfig, TablePruner
from archive import archive_table, restore_archive
from async_reads import patch_proxy_with_async_reads


class StorageStrategyPlugin(PluginHooks):
    """Storage strategy plugin — cache, async reads, archive, pruning.

    Wraps node.storage in StorageCacheProxy immediately on __init__.
    Archive and pruning run on on_tick at configured intervals.
    """

    _PRUNE_INTERVAL_SECONDS = 3600   # Run pruning at most once per hour
    _ARCHIVE_INTERVAL_SECONDS = 3600  # Run archiving at most once per hour

    def __init__(self, ctx: PluginContext, config: dict):
        self._ctx = ctx
        self._log = ctx.log
        self._debug = config.get("debug", False)
        self._config = config

        # TP-5: Defer storage wrapping — ctx._node is None during PluginLoader init.
        # Storage proxy will be created on first on_tick when _node is available.
        self._initialized = False
        self._raw_storage = None
        self._proxy = None

        # SA-06: Pruning config (no _node dependency)
        retention_config = config.get("retention", {})
        self._pruning_config = PruningConfig(retention_config)

        # Archive config (no _node dependency for most fields)
        archive_cfg = config.get("archive", {})
        self._archive_cfg = archive_cfg
        self._compress = bool(archive_cfg.get("compress", True))
        self._encrypt = bool(archive_cfg.get("encrypt", False))
        self._archive_dir = None  # resolved during lazy init

        # Timing
        self._last_prune = 0.0
        self._last_archive = 0.0

    def _lazy_init(self) -> bool:
        """TP-5: Deferred storage wrapping — called on first on_tick when _node is available."""
        if self._ctx._node is None:
            if self._debug:
                logger.info("STORAGE_STRATEGY_DEFERRED_INIT node=not_ready")
            return False

        self._raw_storage = self._ctx._node.storage
        ttl_config = self._config.get("cache", {})
        if self._debug:
            logger.info("STORAGE_STRATEGY_INIT_BEGIN cache_ttl_config=%r", ttl_config)
        self._proxy = StorageCacheProxy(self._raw_storage, ttl_config)

        # SA-02: Patch proxy with thread-offloaded async reads
        patch_proxy_with_async_reads(self._proxy)

        # Replace node.storage with the proxy
        self._ctx._node.storage = self._proxy

        # Resolve archive dir now that _node is available
        data_dir = getattr(self._ctx._node, "_config", {}).get("_data_dir", ".")
        self._archive_dir = os.path.join(
            data_dir, self._archive_cfg.get("archive_dir", "archives")
        )
        if self._debug:
            logger.info("STORAGE_STRATEGY_ARCHIVE_DIR dir=%s", self._archive_dir)

        self._initialized = True
        logger.info("STORAGE_STRATEGY_INIT cache_proxy=enabled async_reads=enabled")
        return True

    async def on_tick(self, peers: List[NodeInfo], health: NodeHealth) -> None:
        """Run periodic archiving and pruning."""
        # TP-5: Deferred init — wait for _node to be available
        if not self._initialized:
            if not self._lazy_init():
                return  # _node still None, skip this tick

        now = time.time()

        # SA-03/SA-04: Archive rotation
        # TP-4: Track which tables were successfully archived
        archived_tables: set = set()
        if now - self._last_archive >= self._ARCHIVE_INTERVAL_SECONDS:
            self._last_archive = now
            if self._debug:
                logger.info("STORAGE_STRATEGY_ARCHIVE_START interval=%.0f", self._ARCHIVE_INTERVAL_SECONDS)
            try:
                archived_tables = await self._run_archive_cycle()
                if self._debug:
                    logger.info("STORAGE_STRATEGY_ARCHIVE_DONE tables=%r", sorted(archived_tables))
            except Exception as exc:
                logger.warning("STORAGE_STRATEGY_ARCHIVE_FAIL error=%s", exc)

        # SA-06: Pruning
        if now - self._last_prune >= self._PRUNE_INTERVAL_SECONDS:
            self._last_prune = now
            if self._debug:
                logger.info("STORAGE_STRATEGY_PRUNE_START interval=%.0f", self._PRUNE_INTERVAL_SECONDS)
            try:
                self._run_pruning_cycle(archived_tables)
                if self._debug:
                    logger.info("STORAGE_STRATEGY_PRUNE_DONE")
            except Exception as exc:
                logger.warning("STORAGE_STRATEGY_PRUNE_FAIL error=%s", exc)

        if self._debug:
            stats = self._proxy.cache_stats()
            hit_rate = (stats["hits"] / max(1, stats["hits"] + stats["misses"])) * 100
            logger.info(
                "STORAGE_CACHE_STATS hits=%d misses=%d size=%d hit_rate=%.1f%% peers=%d write_queue=%d",
                stats["hits"], stats["misses"], stats["size"], hit_rate,
                len(peers), getattr(health, 'write_queue_depth', 0)
            )
            if stats["misses"] > 0 and hit_rate < 50.0:
                logger.info("STORAGE_CACHE_LOW_HIT_RATE rate=%.1f%% — consider increasing cache TTLs", hit_rate)

    async def _run_archive_cycle(self) -> set:
        """SA-03: Archive old rows from receipt_log and execution_log.

        TP-4: Returns set of table names that were successfully archived.
        """
        import asyncio
        conn = self._raw_storage._get_conn()

        vault = None
        sign_fn = None
        if self._encrypt:
            # SA-04: Get vault and sign_fn from ctx if available
            vault = getattr(self._ctx, "vault", None)
            sign_fn = getattr(self._ctx, "sign_bytes", None)

        archived_tables: set = set()
        for table in ("receipt_log", "execution_log"):
            retention = self._pruning_config.retention_seconds(table)
            if retention == 0:
                continue
            cutoff = time.time() - retention
            try:
                rows_archived = await asyncio.to_thread(
                    archive_table,
                    conn, table, cutoff, self._archive_dir,
                    self._compress, vault, sign_fn
                )
                archived_tables.add(table)  # TP-4: mark success
                if rows_archived and self._debug:
                    logger.info("ARCHIVE_ROTATED table=%s rows=%d", table, rows_archived)
            except Exception as exc:
                logger.warning("ARCHIVE_TABLE_FAIL table=%s error=%s", table, exc)

        return archived_tables

    def _run_pruning_cycle(self, archived_tables: set = None) -> None:
        """SA-06: Prune old rows from configured tables.

        TP-4: archived_tables controls which archive-eligible tables may be pruned.
        """
        pruner = TablePruner(
            storage=self._raw_storage,
            pruning_config=self._pruning_config,
            archive_fn=None,
            archived_tables=archived_tables or set(),
        )
        results = pruner.prune_all()
        if results and self._debug:
            for table, count in results.items():
                logger.info("PRUNER_RESULT table=%s deleted=%d", table, count)

    async def on_shutdown(self) -> None:
        """Restore node.storage to raw storage on shutdown."""
        try:
            if self._initialized and self._raw_storage is not None:
                self._ctx._node.storage = self._raw_storage
                logger.info("STORAGE_STRATEGY_SHUTDOWN raw_storage_restored=true")
        except Exception as exc:
            logger.warning("STORAGE_STRATEGY_SHUTDOWN_FAIL error=%s", exc)
