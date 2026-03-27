"""A-02: Protocol/identity EventBus split.

Tests:
1. DHTNode has both `bus` (identity) and `protocol_bus` (protocol) attributes.
2. Protocol-prefix events go to protocol_bus, not identity bus.
3. Identity-prefix events go to identity bus, not protocol bus.
4. _emit_event routes peer.* to protocol_bus.
5. _emit_event routes node.* to protocol_bus.
6. _emit_event routes security.* to protocol_bus.
7. _emit_event routes cache.* to protocol_bus.
8. _emit_event routes skill.registered to protocol_bus.
9. _emit_event routes task.* to identity bus (bus).
10. _emit_event routes mail.* to identity bus (bus).
11. Protocol bus has size=512.
12. PluginContext receives identity bus (not protocol bus).
"""
import pytest
from unittest.mock import MagicMock, patch


class TestBusSplit:
    def test_node_has_protocol_bus(self):
        """DHTNode exposes protocol_bus attribute after init."""
        from knarr.dht.node import DHTNode
        assert hasattr(DHTNode, "__init__"), "DHTNode must be a class"
        # Check via source — node is too complex to instantiate
        import os
        node_path = os.path.join(os.path.dirname(__file__), "../../src/knarr/dht/node.py")
        with open(node_path) as f:
            src = f.read()
        assert "self.protocol_bus" in src, "protocol_bus must be set in __init__"
        assert "EventBus(size=512" in src, "protocol bus must have size=512"

    def test_node_has_identity_bus(self):
        """DHTNode still exposes bus (identity bus) attribute."""
        import os
        node_path = os.path.join(os.path.dirname(__file__), "../../src/knarr/dht/node.py")
        with open(node_path) as f:
            src = f.read()
        # TP-6: bus is now ScopedEventBus wrapping protocol + identity buses
        assert "self.bus = ScopedEventBus" in src, "bus must be ScopedEventBus wrapping both buses"

    def test_emit_event_routes_peer_to_protocol_bus(self):
        """_emit_event sends peer.* to protocol_bus."""
        from knarr.dht.node import DHTNode

        node = DHTNode.__new__(DHTNode)
        node.bus = MagicMock()
        node.protocol_bus = MagicMock()

        node._emit_event("peer.connected", node_id="aa" * 32)

        node.protocol_bus.emit.assert_called_once()
        call_args = node.protocol_bus.emit.call_args
        assert call_args[0][0] == "peer.connected"
        node.bus.emit.assert_not_called()

    def test_emit_event_routes_node_to_protocol_bus(self):
        """_emit_event sends node.* to protocol_bus."""
        from knarr.dht.node import DHTNode

        node = DHTNode.__new__(DHTNode)
        node.bus = MagicMock()
        node.protocol_bus = MagicMock()

        node._emit_event("node.started")

        node.protocol_bus.emit.assert_called_once()
        node.bus.emit.assert_not_called()

    def test_emit_event_routes_security_to_protocol_bus(self):
        """_emit_event sends security.* to protocol_bus."""
        from knarr.dht.node import DHTNode

        node = DHTNode.__new__(DHTNode)
        node.bus = MagicMock()
        node.protocol_bus = MagicMock()

        node._emit_event("security.egress_blocked", skill_name="test")

        node.protocol_bus.emit.assert_called_once()
        node.bus.emit.assert_not_called()

    def test_emit_event_routes_cache_to_protocol_bus(self):
        """_emit_event sends cache.* to protocol_bus."""
        from knarr.dht.node import DHTNode

        node = DHTNode.__new__(DHTNode)
        node.bus = MagicMock()
        node.protocol_bus = MagicMock()

        node._emit_event("cache.fill.acl.peer", acl={})

        node.protocol_bus.emit.assert_called_once()
        node.bus.emit.assert_not_called()

    def test_emit_event_routes_skill_registered_to_protocol_bus(self):
        """_emit_event sends skill.registered to protocol_bus."""
        from knarr.dht.node import DHTNode

        node = DHTNode.__new__(DHTNode)
        node.bus = MagicMock()
        node.protocol_bus = MagicMock()

        node._emit_event("skill.registered", skill_name="test")

        node.protocol_bus.emit.assert_called_once()
        node.bus.emit.assert_not_called()

    def test_emit_event_routes_skill_removed_to_protocol_bus(self):
        """_emit_event sends skill.removed to protocol_bus."""
        from knarr.dht.node import DHTNode

        node = DHTNode.__new__(DHTNode)
        node.bus = MagicMock()
        node.protocol_bus = MagicMock()

        node._emit_event("skill.removed", skill_name="test")

        node.protocol_bus.emit.assert_called_once()
        node.bus.emit.assert_not_called()

    def test_emit_event_routes_task_to_identity_bus(self):
        """_emit_event sends task.* to identity bus (self.bus)."""
        from knarr.dht.node import DHTNode

        node = DHTNode.__new__(DHTNode)
        node.bus = MagicMock()
        node.protocol_bus = MagicMock()

        node._emit_event("task.completed", skill_name="test")

        node.bus.emit.assert_called_once()
        node.protocol_bus.emit.assert_not_called()

    def test_emit_event_routes_mail_to_identity_bus(self):
        """_emit_event sends mail.* to identity bus (self.bus)."""
        from knarr.dht.node import DHTNode

        node = DHTNode.__new__(DHTNode)
        node.bus = MagicMock()
        node.protocol_bus = MagicMock()

        node._emit_event("mail.flush_skip", reason="test")

        node.bus.emit.assert_called_once()
        node.protocol_bus.emit.assert_not_called()

    def test_emit_event_routes_receipt_to_identity_bus(self):
        """_emit_event sends receipt.* to identity bus."""
        from knarr.dht.node import DHTNode

        node = DHTNode.__new__(DHTNode)
        node.bus = MagicMock()
        node.protocol_bus = MagicMock()

        node._emit_event("receipt.write_failed", receipt_id="r1")

        node.bus.emit.assert_called_once()
        node.protocol_bus.emit.assert_not_called()

    def test_plugin_context_gets_identity_bus(self):
        """PluginLoader wires PluginContext to identity bus (self.bus)."""
        import os
        node_path = os.path.join(os.path.dirname(__file__), "../../src/knarr/dht/node.py")
        with open(node_path) as f:
            src = f.read()
        # The plugin loader should wire subscribe_events to self.bus.subscribe
        # and emit_event to self.bus.emit (identity bus)
        assert "subscribe_events_cb=self.bus.subscribe" in src
        assert "emit_event_cb=self.bus.emit" in src
        assert "bus=self.bus" in src
