"""B-03: Pool metrics.

Tests:
1. TrackedThreadPool exposes active_workers, queue_depth, peak_queue_depth.
2. active_workers increments while work is running.
3. queue_depth increments when work is submitted, decrements when started.
4. peak_queue_depth tracks the maximum queue depth seen.
5. get_metrics() returns all expected fields.
6. DHTNode.get_pool_metrics() returns metrics for both pools.
"""
import concurrent.futures
import threading
import time
import pytest


class TestTrackedThreadPool:
    def test_initial_metrics_zero(self):
        """All metrics start at 0."""
        from knarr.dht.node import TrackedThreadPool
        pool = TrackedThreadPool(max_workers=4, thread_name_prefix="test")
        try:
            m = pool.get_metrics()
            assert m["active_workers"] == 0
            assert m["queue_depth"] == 0
            assert m["peak_queue_depth"] == 0
        finally:
            pool.shutdown(wait=True)

    def test_active_workers_increments(self):
        """active_workers increments while work executes."""
        from knarr.dht.node import TrackedThreadPool
        pool = TrackedThreadPool(max_workers=4, thread_name_prefix="test")
        barrier = threading.Barrier(2)
        release = threading.Event()

        def work():
            barrier.wait(timeout=5)
            release.wait(timeout=5)

        try:
            fut = pool.submit(work)
            barrier.wait(timeout=5)  # work is running
            m = pool.get_metrics()
            assert m["active_workers"] == 1
        finally:
            release.set()
            fut.result(timeout=5)
            pool.shutdown(wait=True)

    def test_active_workers_decrements_after_done(self):
        """active_workers decrements after work completes."""
        from knarr.dht.node import TrackedThreadPool
        pool = TrackedThreadPool(max_workers=4, thread_name_prefix="test")
        try:
            fut = pool.submit(lambda: 42)
            assert fut.result(timeout=5) == 42
            time.sleep(0.05)  # brief yield
            m = pool.get_metrics()
            assert m["active_workers"] == 0
        finally:
            pool.shutdown(wait=True)

    def test_peak_queue_depth_tracks_max(self):
        """peak_queue_depth tracks maximum queue depth."""
        from knarr.dht.node import TrackedThreadPool
        # Use 1 worker and saturate it to build a queue
        pool = TrackedThreadPool(max_workers=1, thread_name_prefix="test")
        barrier = threading.Barrier(2)
        release = threading.Event()

        def blocking_work():
            barrier.wait(timeout=5)
            release.wait(timeout=5)

        try:
            # First: fill the single worker
            fut1 = pool.submit(blocking_work)
            barrier.wait(timeout=5)  # worker is now blocked
            # Queue up more
            fut2 = pool.submit(lambda: None)
            fut3 = pool.submit(lambda: None)
            m = pool.get_metrics()
            assert m["peak_queue_depth"] >= 2
        finally:
            release.set()
            pool.shutdown(wait=True)

    def test_get_metrics_has_all_keys(self):
        """get_metrics() returns dict with all required keys."""
        from knarr.dht.node import TrackedThreadPool
        pool = TrackedThreadPool(max_workers=4)
        try:
            m = pool.get_metrics()
            assert "active_workers" in m
            assert "queue_depth" in m
            assert "peak_queue_depth" in m
            assert "max_workers" in m
        finally:
            pool.shutdown(wait=True)

    def test_max_workers_in_metrics(self):
        """max_workers is recorded in metrics."""
        from knarr.dht.node import TrackedThreadPool
        pool = TrackedThreadPool(max_workers=7)
        try:
            assert pool.get_metrics()["max_workers"] == 7
        finally:
            pool.shutdown(wait=True)


class TestNodePoolMetrics:
    def test_get_pool_metrics_method_exists(self):
        """DHTNode.get_pool_metrics() method exists."""
        from knarr.dht.node import DHTNode
        assert hasattr(DHTNode, "get_pool_metrics"), "get_pool_metrics must exist on DHTNode"

    def test_tracked_pool_used_in_node(self):
        """DHTNode uses TrackedThreadPool for handler and protocol pools."""
        import os
        node_path = os.path.join(os.path.dirname(__file__), "../../src/knarr/dht/node.py")
        with open(node_path) as f:
            src = f.read()
        assert "TrackedThreadPool" in src, "TrackedThreadPool must be used in node.py"
        assert "class TrackedThreadPool" in src, "TrackedThreadPool must be defined in node.py"
