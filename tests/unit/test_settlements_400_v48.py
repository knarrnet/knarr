"""Tests for B1-EXT: non-integer limit/offset returns 400."""
import asyncio
import json
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


class TestSettlements400(unittest.TestCase):
    """_handle_settlements_list must return 400 for non-integer limit or offset."""

    def _make_server(self):
        """Create a minimal CockpitServer mock for testing _handle_settlements_list."""
        from knarr.dashboard.server import CockpitServer

        node = MagicMock()
        node._config = {}
        node.node_info.node_id = "test_node"

        server = CockpitServer.__new__(CockpitServer)
        server._node = node
        server._log_handler = MagicMock()
        return server

    def _call_settlements(self, server, query):
        """Call _handle_settlements_list and capture the response."""
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()

        # Capture _respond_error calls
        responses = []
        original_respond_error = server._respond_error

        def capture_respond_error(w, status_code, message):
            responses.append(("error", status_code, message))

        server._respond_error = capture_respond_error

        # Also capture _respond_json calls (success path)
        def capture_respond_json(w, data):
            responses.append(("json", 200, data))

        server._respond_json = capture_respond_json

        _run_async(server._handle_settlements_list(writer, query))
        return responses

    def test_non_integer_limit_returns_400(self):
        """limit='abc' must trigger 400, not fall through to DB."""
        server = self._make_server()
        responses = self._call_settlements(server, {"limit": ["abc"]})
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0][0], "error")
        self.assertEqual(responses[0][1], 400)

    def test_non_integer_offset_returns_400(self):
        """offset='xyz' must trigger 400."""
        server = self._make_server()
        responses = self._call_settlements(server, {"offset": ["xyz"]})
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0][0], "error")
        self.assertEqual(responses[0][1], 400)

    def test_float_limit_returns_400(self):
        """limit='3.5' must trigger 400 (int() rejects floats-as-string)."""
        server = self._make_server()
        responses = self._call_settlements(server, {"limit": ["3.5"]})
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0][0], "error")
        self.assertEqual(responses[0][1], 400)

    def test_empty_string_limit_returns_400(self):
        """limit='' must trigger 400."""
        server = self._make_server()
        responses = self._call_settlements(server, {"limit": [""]})
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0][0], "error")
        self.assertEqual(responses[0][1], 400)

    def test_valid_integers_do_not_return_400(self):
        """Valid integer limit and offset must NOT trigger 400 (success path)."""
        server = self._make_server()
        # Mock the DB so the success path doesn't crash
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        conn.execute.return_value.fetchone.return_value = (0,)
        server._node.storage._get_conn.return_value = conn

        responses = self._call_settlements(server, {"limit": ["10"], "offset": ["0"]})
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0][0], "json")
        self.assertEqual(responses[0][1], 200)


if __name__ == "__main__":
    unittest.main()
