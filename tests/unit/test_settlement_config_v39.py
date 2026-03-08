"""Tests for settlement config resolution (v0.39.2 fix).

Verifies _get_settlement_config resolves from both [settlement] (top-level)
and [economy.settlement] (nested) config paths.
"""

import unittest
from unittest.mock import MagicMock


def _make_node(config: dict):
    """Create a minimal mock node with config for testing _get_settlement_config."""
    from knarr.dht.node import DHTNode
    # We can't instantiate DHTNode fully, so test the method logic directly
    node = MagicMock(spec=DHTNode)
    node._config = config
    # Bind the real method to our mock
    node._get_settlement_config = DHTNode._get_settlement_config.__get__(node)
    return node


class TestSettlementConfigResolution(unittest.TestCase):
    """_get_settlement_config must resolve from [settlement] or [economy.settlement]."""

    def test_top_level_settlement(self):
        """Config with [settlement] at top level — primary path."""
        node = _make_node({
            "settlement": {"tab_reminder_threshold": 75.0, "netting_interval": 1800},
        })
        cfg = node._get_settlement_config()
        assert cfg["tab_reminder_threshold"] == 75.0
        assert cfg["netting_interval"] == 1800

    def test_nested_economy_settlement(self):
        """Config with [economy.settlement] — fallback path (Viggo's VPS pattern)."""
        node = _make_node({
            "economy": {"settlement": {"tab_reminder_threshold": 60.0, "consumer_interval": 30}},
        })
        cfg = node._get_settlement_config()
        assert cfg["tab_reminder_threshold"] == 60.0
        assert cfg["consumer_interval"] == 30

    def test_top_level_takes_precedence(self):
        """If both paths exist, [settlement] wins over [economy.settlement]."""
        node = _make_node({
            "settlement": {"tab_reminder_threshold": 90.0},
            "economy": {"settlement": {"tab_reminder_threshold": 50.0}},
        })
        cfg = node._get_settlement_config()
        assert cfg["tab_reminder_threshold"] == 90.0

    def test_empty_config_returns_empty(self):
        """No settlement config anywhere — returns empty dict (defaults apply at call sites)."""
        node = _make_node({})
        cfg = node._get_settlement_config()
        assert cfg == {}

    def test_economy_without_settlement_returns_empty(self):
        """[economy] exists but no settlement sub-section."""
        node = _make_node({"economy": {"default_soft_limit": -5.0}})
        cfg = node._get_settlement_config()
        assert cfg == {}

    def test_empty_top_level_falls_through(self):
        """[settlement] exists but is empty — falls through to [economy.settlement]."""
        node = _make_node({
            "settlement": {},
            "economy": {"settlement": {"netting_interval": 7200}},
        })
        cfg = node._get_settlement_config()
        assert cfg["netting_interval"] == 7200

    def test_consumer_interval_resolved(self):
        """consumer_interval (v0.39.1 addition) resolved from config."""
        node = _make_node({
            "settlement": {"consumer_interval": 120},
        })
        cfg = node._get_settlement_config()
        assert cfg["consumer_interval"] == 120


class TestSettlementConfigUsageSites(unittest.TestCase):
    """Verify the 5 call sites use _get_settlement_config (not raw config)."""

    def test_no_raw_settlement_reads_remain(self):
        """Grep guard: node.py must not have self._config.get('settlement') outside the helper."""
        import inspect
        from knarr.dht.node import DHTNode

        source = inspect.getsource(DHTNode)
        # Remove the helper method definition itself
        helper_def = 'def _get_settlement_config(self)'
        # Count raw reads (should only appear inside _get_settlement_config)
        raw_reads = source.count('self._config.get("settlement"')
        # The helper itself has exactly 1 raw read
        assert raw_reads == 1, (
            f"Found {raw_reads} raw self._config.get('settlement') reads. "
            f"Expected exactly 1 (inside _get_settlement_config helper). "
            f"All other reads must use self._get_settlement_config()."
        )


if __name__ == "__main__":
    unittest.main()
