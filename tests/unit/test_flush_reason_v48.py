"""Tests for A5-EXT: mail.flush_skip event includes reason kwarg."""
import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        except Exception:
            pass
        asyncio.set_event_loop(asyncio.new_event_loop())


class TestFlushSkipReason(unittest.TestCase):
    """mail.flush_skip must include reason='no_route' when peer is unreachable."""

    def _make_engine(self):
        """Create a SyncEngine with mocked node for flush_outbox testing."""
        from knarr.mail.sync import SyncEngine

        node = MagicMock()
        node._config = {"mail": {"debug": False}}
        node.bus = MagicMock()
        # No peers in table
        node.storage.get_peers.return_value = []
        # No provider address fallback
        node.storage.get_provider_address.return_value = None
        # No peer record for the recipient (forces no_route branch)
        node.storage.get_peer_by_id.return_value = None
        # resolve_peer returns dummy (no override match)
        node.resolve_peer.return_value = ("0.0.0.0", 0)
        # node_info.node_id so self-delivery check doesn't match
        node.node_info.node_id = "self_node_id"

        engine = SyncEngine(node)
        engine.push_to_peer = AsyncMock()
        return engine, node

    def test_flush_skip_emits_reason_no_route(self):
        """When flush_outbox skips a peer (no route), the bus event includes reason='no_route'."""
        engine, node = self._make_engine()

        fake_to_node = "a" * 64
        node.storage.get_outbox_recipients.return_value = [fake_to_node]

        _run_async(engine.flush_outbox())

        # Verify bus.emit was called with reason="no_route"
        calls = [c for c in node.bus.emit.call_args_list if c[0][0] == "mail.flush_skip"]
        self.assertGreaterEqual(len(calls), 1, "Expected at least one mail.flush_skip event")
        call_kwargs = calls[0][1]
        self.assertEqual(call_kwargs.get("reason"), "no_route",
                         f"Expected reason='no_route', got {call_kwargs}")

    def test_flush_skip_still_includes_to_node(self):
        """Existing to_node kwarg must still be present alongside reason."""
        engine, node = self._make_engine()

        fake_to_node = "b" * 64
        node.storage.get_outbox_recipients.return_value = [fake_to_node]

        _run_async(engine.flush_outbox())

        calls = [c for c in node.bus.emit.call_args_list if c[0][0] == "mail.flush_skip"]
        self.assertGreaterEqual(len(calls), 1)
        call_kwargs = calls[0][1]
        self.assertIn("to_node", call_kwargs, "to_node kwarg must still be present")
        self.assertIn("reason", call_kwargs, "reason kwarg must be present")


if __name__ == "__main__":
    unittest.main()
