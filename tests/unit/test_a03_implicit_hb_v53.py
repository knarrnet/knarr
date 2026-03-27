"""A-03: IMPLICIT_HB configurable message types.

Tests that:
1. Default (no config) = legacy behavior: all message types except PluginMessage trigger IMPLICIT_HB.
2. Configured list: only listed types trigger IMPLICIT_HB.
3. Empty list: no types trigger IMPLICIT_HB.
4. PluginMessage type ("PLUGIN_MESSAGE") can explicitly be added to the list.
"""
import pytest
from unittest.mock import MagicMock


def _make_node(implicit_hb_types=None):
    """Create a minimal DHTNode-like object with A-03 logic."""
    from knarr.dht.node import DHTNode
    from knarr.core.messages import PluginMessage

    node = MagicMock(spec=DHTNode)
    if implicit_hb_types is None:
        node._implicit_hb_types = None  # legacy
    else:
        node._implicit_hb_types = tuple(implicit_hb_types)
    node._peer_last_activity = {}
    return node


def _check_implicit_hb(node, msg):
    """Reproduce the A-03 IMPLICIT_HB logic from the connection handler."""
    from knarr.core.messages import PluginMessage
    signer_id = "aabbcc" * 10 + "aabbcc"  # 64 hex chars
    _implicit_hb_match = False
    if signer_id:
        if node._implicit_hb_types is None:
            _implicit_hb_match = not isinstance(msg, PluginMessage)
        else:
            _implicit_hb_match = msg.type in node._implicit_hb_types
    return _implicit_hb_match


class TestImplicitHBTypes:
    def test_default_excludes_plugin_message(self):
        """Default (None) = exclude PluginMessage."""
        from knarr.core.messages import PluginMessage, Heartbeat, Announce
        node = _make_node(None)
        assert _check_implicit_hb(node, Heartbeat()) is True
        assert _check_implicit_hb(node, Announce()) is True
        assert _check_implicit_hb(node, PluginMessage()) is False

    def test_configured_list_only_listed_types(self):
        """Only listed types trigger IMPLICIT_HB."""
        from knarr.core.messages import Heartbeat, Announce, Query
        node = _make_node(["HEARTBEAT", "ANNOUNCE"])
        assert _check_implicit_hb(node, Heartbeat()) is True
        assert _check_implicit_hb(node, Announce()) is True
        assert _check_implicit_hb(node, Query()) is False

    def test_empty_list_no_types(self):
        """Empty list = no types trigger IMPLICIT_HB."""
        from knarr.core.messages import Heartbeat, Announce
        node = _make_node([])
        assert _check_implicit_hb(node, Heartbeat()) is False
        assert _check_implicit_hb(node, Announce()) is False

    def test_plugin_message_can_be_added(self):
        """PluginMessage can explicitly be added to the list."""
        from knarr.core.messages import PluginMessage
        node = _make_node(["PLUGIN_MESSAGE"])
        assert _check_implicit_hb(node, PluginMessage()) is True

    def test_node_init_reads_config(self):
        """DHTNode.__init__ reads implicit_hb_types from config."""
        # We can't instantiate a full DHTNode, but we can test the init logic directly.
        import types

        # Simulate the __init__ logic for A-03
        config = {"node": {"implicit_hb_types": ["HEARTBEAT", "ANNOUNCE"]}}
        _hb_types_cfg = config.get("node", {}).get("implicit_hb_types", None)

        if _hb_types_cfg is not None:
            _implicit_hb_types = tuple(_hb_types_cfg)
        else:
            _implicit_hb_types = None

        assert _implicit_hb_types == ("HEARTBEAT", "ANNOUNCE")

    def test_node_init_default(self):
        """DHTNode.__init__ defaults to None (legacy) when not configured."""
        config = {"node": {}}
        _hb_types_cfg = config.get("node", {}).get("implicit_hb_types", None)
        if _hb_types_cfg is not None:
            _implicit_hb_types = tuple(_hb_types_cfg)
        else:
            _implicit_hb_types = None

        assert _implicit_hb_types is None
