"""B-02: Bus metrics.

Tests:
1. ring_fill_pct: percentage of ring buffer filled.
2. events_dropped_count: counts drops when ring overflows.
3. deferred_queue_depth: counts deferred events.
4. subscribers_behind_count: counts subscribers behind the oldest entry.
5. get_metrics() returns all metrics as dict.
6. Metrics logged at 60s when >50% full.
"""
import asyncio
import time
import pytest
from knarr.dht.eventbus import EventBus


class TestBusMetrics:
    def test_ring_fill_pct_empty(self):
        """Empty bus has 0% fill."""
        bus = EventBus(size=16)
        assert bus.ring_fill_pct == 0.0

    def test_ring_fill_pct_partial(self):
        """Partially filled bus has correct pct."""
        bus = EventBus(size=16)
        for i in range(8):
            bus.emit(f"test.event{i}")
        # 8 of 16 = 50%
        assert abs(bus.ring_fill_pct - 50.0) < 1.0

    def test_ring_fill_pct_full(self):
        """Full bus reports 100% fill."""
        bus = EventBus(size=16)
        for i in range(16):
            bus.emit(f"test.event{i}")
        assert bus.ring_fill_pct == 100.0

    def test_events_dropped_count_zero_on_empty(self):
        """No drops when ring has capacity."""
        bus = EventBus(size=64)
        for i in range(32):
            bus.emit(f"test.{i}")
        assert bus.events_dropped_count == 0

    def test_events_dropped_count_increments_on_overflow(self):
        """Drop count increments when ring overflows."""
        size = 16  # min size after clamping
        bus = EventBus(size=size)
        assert bus._size == size  # verify clamping worked
        # Fill the ring
        for i in range(size):
            bus.emit(f"test.fill.{i}")
        # Now overflow — each additional emit overwrites one slot
        overflow = 4
        for i in range(overflow):
            bus.emit(f"test.overflow.{i}")
        assert bus.events_dropped_count >= overflow

    def test_deferred_queue_depth_zero(self):
        """No deferred events initially."""
        bus = EventBus(size=64)
        assert bus.deferred_queue_depth == 0

    def test_deferred_queue_depth_counts_deferred(self):
        """Deferred queue depth reflects scheduled events."""
        bus = EventBus(size=64)
        future_time = time.time() + 1000  # far in future
        bus.emit("test.deferred", valid_from=future_time)
        bus.emit("test.deferred2", valid_from=future_time + 1)
        assert bus.deferred_queue_depth == 2

    def test_subscribers_behind_count_zero_fresh(self):
        """Fresh subscriber is not behind."""
        bus = EventBus(size=64)
        sub = bus.subscribe("test.*")
        assert bus.subscribers_behind_count == 0

    def test_subscribers_behind_count_when_behind(self):
        """Subscriber behind oldest ring entry is counted."""
        size = 16  # min size after clamping
        bus = EventBus(size=size)
        sub = bus.subscribe("test.*")
        # Overflow the ring so the subscriber's cursor falls behind
        for i in range(size + 4):  # 4 more than ring size
            bus.emit(f"test.{i}")
        assert bus.subscribers_behind_count >= 1

    def test_get_metrics_returns_dict(self):
        """get_metrics() returns a dict with all expected keys."""
        bus = EventBus(size=32)
        bus.emit("test.event")
        metrics = bus.get_metrics()
        assert isinstance(metrics, dict)
        assert "ring_fill_pct" in metrics
        assert "events_dropped_count" in metrics
        assert "deferred_queue_depth" in metrics
        assert "subscribers_behind_count" in metrics
        assert "ring_size" in metrics
        assert "head" in metrics
        assert "subscriber_count" in metrics

    def test_get_metrics_values_consistent(self):
        """get_metrics() values are consistent with individual properties."""
        bus = EventBus(size=32)
        for i in range(10):
            bus.emit(f"test.{i}")
        metrics = bus.get_metrics()
        assert metrics["ring_fill_pct"] == bus.ring_fill_pct
        assert metrics["events_dropped_count"] == bus.events_dropped_count
        assert metrics["deferred_queue_depth"] == bus.deferred_queue_depth
        assert metrics["subscribers_behind_count"] == bus.subscribers_behind_count

    def test_drop_count_after_cancel(self):
        """Cancelling deferred event removes it from deferred queue."""
        bus = EventBus(size=64)
        eid = bus.emit("test.deferred", valid_from=time.time() + 1000)
        assert bus.deferred_queue_depth == 1
        bus.cancel(eid)
        assert bus.deferred_queue_depth == 0
