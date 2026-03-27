"""C-06: Thrall commerce local-mode bypass.

Tests the PluginContext.get_economy_stats() helper:
1. Returns storage stats when node.storage.get_economy_stats() is available.
2. Returns None when _node is None (legacy path).
3. Returns None when storage has no get_economy_stats method.
4. Returns None when get_economy_stats raises an exception.
"""
import pytest
from unittest.mock import MagicMock


class TestGetEconomyStats:
    def test_returns_stats_from_node_storage(self):
        """Returns stats dict when node.storage.get_economy_stats() is available."""
        from knarr.dht.plugins import PluginContext

        expected = {"balance": 100, "total_sent": 50, "total_received": 150}
        node = MagicMock()
        node.storage.get_economy_stats.return_value = expected

        ctx = PluginContext(node=node, node_id="aa" * 32)
        result = ctx.get_economy_stats()
        assert result == expected
        node.storage.get_economy_stats.assert_called_once()

    def test_returns_none_when_no_node(self):
        """Returns None when constructed without node (legacy path)."""
        from knarr.dht.plugins import PluginContext

        ctx = PluginContext(node_id="bb" * 32)
        result = ctx.get_economy_stats()
        assert result is None

    def test_returns_none_when_node_has_no_storage(self):
        """Returns None when node has no storage attribute."""
        from knarr.dht.plugins import PluginContext

        node = MagicMock(spec=[])  # no attributes allowed
        ctx = PluginContext.__new__(PluginContext)
        ctx._node = node

        result = ctx.get_economy_stats()
        assert result is None

    def test_returns_none_when_storage_has_no_method(self):
        """Returns None when storage does not have get_economy_stats."""
        from knarr.dht.plugins import PluginContext

        node = MagicMock()
        # Remove get_economy_stats from storage mock
        del node.storage.get_economy_stats

        ctx = PluginContext(node=node, node_id="cc" * 32)
        result = ctx.get_economy_stats()
        assert result is None

    def test_returns_none_on_storage_exception(self):
        """Returns None when get_economy_stats raises an exception."""
        from knarr.dht.plugins import PluginContext

        node = MagicMock()
        node.storage.get_economy_stats.side_effect = RuntimeError("DB locked")

        ctx = PluginContext(node=node, node_id="dd" * 32)
        result = ctx.get_economy_stats()
        assert result is None

    def test_method_exists_on_plugin_context(self):
        """get_economy_stats method is present on PluginContext."""
        from knarr.dht.plugins import PluginContext
        assert hasattr(PluginContext, "get_economy_stats")
        assert callable(PluginContext.get_economy_stats)

    def test_returns_none_when_node_is_none_explicitly(self):
        """Returns None when _node is explicitly set to None."""
        from knarr.dht.plugins import PluginContext

        ctx = PluginContext.__new__(PluginContext)
        ctx._node = None

        result = ctx.get_economy_stats()
        assert result is None
