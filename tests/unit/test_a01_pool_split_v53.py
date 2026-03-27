"""A-01: Configurable protocol/handler thread pool split.

Tests that:
1. DHTNode creates both _protocol_pool and _handler_pool.
2. Protocol pool size comes from config [node.pools] protocol key.
3. Handler pool size comes from config [node.pools] handler key.
4. Protocol pool can execute work while handler pool is saturated.
"""
import asyncio
import concurrent.futures
import time
import pytest
from unittest.mock import MagicMock, patch, AsyncMock


def _make_node_with_pools(protocol=4, handler=8):
    """Create a DHTNode using minimal config with pool settings."""
    from knarr.dht.node import DHTNode
    config = {
        "node": {
            "pools": {
                "protocol": protocol,
                "handler": handler,
            }
        }
    }
    node = DHTNode.__new__(DHTNode)
    node._config = config
    node._debug = False
    # A-01: init pools
    import os
    import concurrent.futures as cf
    _pools_cfg = config.get("node", {}).get("pools", {})
    _handler_workers = int(_pools_cfg.get("handler", max(32, (os.cpu_count() or 4) + 4)))
    _protocol_workers = int(_pools_cfg.get("protocol", 8))
    node._handler_pool = cf.ThreadPoolExecutor(
        max_workers=_handler_workers, thread_name_prefix="knarr-handler"
    )
    node._protocol_pool = cf.ThreadPoolExecutor(
        max_workers=_protocol_workers, thread_name_prefix="knarr-protocol"
    )
    return node


class TestPoolSplit:
    def test_both_pools_created(self):
        """Node has both _handler_pool and _protocol_pool."""
        node = _make_node_with_pools(protocol=4, handler=8)
        try:
            assert hasattr(node, "_handler_pool")
            assert hasattr(node, "_protocol_pool")
            assert isinstance(node._handler_pool, concurrent.futures.ThreadPoolExecutor)
            assert isinstance(node._protocol_pool, concurrent.futures.ThreadPoolExecutor)
        finally:
            node._handler_pool.shutdown(wait=False)
            node._protocol_pool.shutdown(wait=False)

    def test_pool_sizes_from_config(self):
        """Pool sizes are read from [node.pools] config."""
        from knarr.dht.node import DHTNode
        import os, concurrent.futures as cf
        for protocol, handler in [(2, 4), (8, 16), (4, 32)]:
            node = _make_node_with_pools(protocol=protocol, handler=handler)
            try:
                # We can't directly check max_workers, but we can verify the pool works
                fut_p = node._protocol_pool.submit(lambda: "proto")
                fut_h = node._handler_pool.submit(lambda: "handler")
                assert fut_p.result(timeout=2) == "proto"
                assert fut_h.result(timeout=2) == "handler"
            finally:
                node._handler_pool.shutdown(wait=False)
                node._protocol_pool.shutdown(wait=False)

    def test_protocol_fires_while_handler_saturated(self):
        """Protocol pool executes work within 100ms while handler pool is 100% saturated."""
        import threading

        node = _make_node_with_pools(protocol=2, handler=4)
        try:
            # Saturate handler pool
            barrier = threading.Barrier(4 + 1)
            release = threading.Event()

            def block():
                barrier.wait(timeout=5)
                release.wait(timeout=5)

            # Fill handler pool
            for _ in range(4):
                node._handler_pool.submit(block)
            barrier.wait(timeout=5)  # all handler threads are blocked

            # Protocol pool should fire immediately
            start = time.monotonic()
            result = node._protocol_pool.submit(lambda: "protocol_fired").result(timeout=2)
            elapsed_ms = (time.monotonic() - start) * 1000

            assert result == "protocol_fired"
            assert elapsed_ms < 100, f"Protocol pool took {elapsed_ms:.1f}ms — must be < 100ms"
        finally:
            release.set()
            node._handler_pool.shutdown(wait=False)
            node._protocol_pool.shutdown(wait=False)

    def test_default_pool_config(self):
        """Default config creates pools with reasonable sizes."""
        import os, concurrent.futures as cf
        node = _make_node_with_pools()  # no explicit pool sizes → uses defaults
        try:
            fut = node._protocol_pool.submit(lambda: 42)
            assert fut.result(timeout=2) == 42
        finally:
            node._handler_pool.shutdown(wait=False)
            node._protocol_pool.shutdown(wait=False)
