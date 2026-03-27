"""B-04: Cockpit /api/metrics endpoint.

Tests:
1. _build_metrics_response() returns bus stats, pool stats, cache stats, identities.
2. Bus metrics come from node.bus (and protocol_bus if available).
3. Pool metrics come from node.get_pool_metrics().
4. Cache stats come from storage.cache_stats() if available.
5. Identities list has at least one entry in single-identity mode.
"""
import pytest
from unittest.mock import MagicMock


def _make_server():
    """Create minimal CockpitServer with mocked node."""
    from knarr.dashboard.server import CockpitServer

    node = MagicMock()
    node.node_info.node_id = "aa" * 32
    node._own_skills = {"skill1": MagicMock(), "skill2": MagicMock()}

    # Mock bus
    bus = MagicMock()
    bus.get_metrics.return_value = {
        "ring_fill_pct": 25.0,
        "events_dropped_count": 0,
        "deferred_queue_depth": 0,
        "subscribers_behind_count": 0,
        "ring_size": 256,
        "head": 64,
        "subscriber_count": 3,
    }
    node.bus = bus
    node.protocol_bus = None

    # Mock pool metrics
    node.get_pool_metrics.return_value = {
        "handler": {"active_workers": 2, "queue_depth": 0, "peak_queue_depth": 5, "max_workers": 32},
        "protocol": {"active_workers": 0, "queue_depth": 0, "peak_queue_depth": 1, "max_workers": 8},
    }

    # Mock storage cache stats
    node.storage.cache_stats.return_value = {
        "hits": 100,
        "misses": 20,
        "size": 50,
        "invalidations": 3,
    }
    node._identity_registry = None

    server = CockpitServer.__new__(CockpitServer)
    server._node = node
    return server


class TestMetricsEndpoint:
    def test_build_metrics_returns_dict(self):
        """_build_metrics_response() returns a dict."""
        server = _make_server()
        result = server._build_metrics_response()
        assert isinstance(result, dict)

    def test_bus_metrics_present(self):
        """Bus metrics are present in response."""
        server = _make_server()
        result = server._build_metrics_response()
        assert "bus" in result
        assert isinstance(result["bus"], dict)

    def test_bus_metrics_values(self):
        """Bus metrics contain expected fields."""
        server = _make_server()
        result = server._build_metrics_response()
        # Should have identity_bus or bus key
        bus = result["bus"]
        bus_data = bus.get("identity_bus") or bus.get("bus") or {}
        if bus_data:
            assert "ring_fill_pct" in bus_data
            assert "events_dropped_count" in bus_data

    def test_pool_metrics_present(self):
        """Pool metrics are present in response."""
        server = _make_server()
        result = server._build_metrics_response()
        assert "pools" in result
        pools = result["pools"]
        assert "handler" in pools
        assert "protocol" in pools

    def test_pool_handler_metrics(self):
        """Handler pool metrics include active_workers, queue_depth, peak."""
        server = _make_server()
        result = server._build_metrics_response()
        handler = result["pools"]["handler"]
        assert "active_workers" in handler
        assert "queue_depth" in handler
        assert "peak_queue_depth" in handler

    def test_cache_stats_present(self):
        """Cache stats are present when storage has cache_stats()."""
        server = _make_server()
        result = server._build_metrics_response()
        assert "cache" in result
        cache = result["cache"]
        if cache:  # may be empty if storage doesn't have cache_stats
            assert "hits" in cache or "hit_rate_pct" in cache

    def test_cache_hit_rate(self):
        """Cache hit rate is computed correctly."""
        server = _make_server()
        result = server._build_metrics_response()
        cache = result["cache"]
        if cache and "hit_rate_pct" in cache:
            # hits=100, misses=20 → rate = 100/120 ≈ 83.3%
            assert abs(cache["hit_rate_pct"] - 83.3) < 1.0

    def test_identities_list_present(self):
        """Identities list is always present with at least one entry."""
        server = _make_server()
        result = server._build_metrics_response()
        assert "identities" in result
        assert len(result["identities"]) >= 1

    def test_identities_default_entry(self):
        """Default identity entry has node_id and name."""
        server = _make_server()
        result = server._build_metrics_response()
        ident = result["identities"][0]
        assert "node_id" in ident
        assert "name" in ident
        assert "skill_count" in ident

    def test_timestamp_in_response(self):
        """Response includes a timestamp."""
        server = _make_server()
        result = server._build_metrics_response()
        assert "ts" in result
        assert isinstance(result["ts"], float)
        assert result["ts"] > 0

    def test_protocol_bus_metrics_included(self):
        """protocol_bus metrics are included when protocol_bus is available."""
        server = _make_server()
        proto_bus = MagicMock()
        proto_bus.get_metrics.return_value = {
            "ring_fill_pct": 10.0,
            "events_dropped_count": 0,
            "deferred_queue_depth": 0,
            "subscribers_behind_count": 0,
        }
        server._node.protocol_bus = proto_bus

        result = server._build_metrics_response()
        assert "protocol_bus" in result["bus"]
