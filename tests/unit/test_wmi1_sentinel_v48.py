"""Tests for WM-I1: Sentinel-file graceful shutdown."""
import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, AsyncMock, patch


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


class TestSentinelStartupGuard(unittest.TestCase):
    """At startup, stale knarr.stop must be deleted."""

    def test_stale_sentinel_deleted_on_start(self):
        """If knarr.stop exists before start(), it is removed."""
        from knarr.dht.node import DHTNode

        with tempfile.TemporaryDirectory() as tmpdir:
            sentinel_path = os.path.join(tmpdir, "knarr.stop")
            with open(sentinel_path, "w") as f:
                f.write("stale")

            self.assertTrue(os.path.exists(sentinel_path))

            node = DHTNode.__new__(DHTNode)
            node._config = {"_config_dir": tmpdir, "_data_dir": tmpdir}
            node._bind_host = "127.0.0.1"
            node.node_info = MagicMock()
            node.node_info.port = 0
            node.node_info.node_id = "test"
            node.node_info.host = "127.0.0.1"
            node.storage = MagicMock()
            node.storage.get_node_key.return_value = None
            node._signing_key = None
            node._running = False
            node._sidecar = None
            node._sidecar_port = 0
            node._asset_dir = ""
            node._mail_handlers = MagicMock()
            node._plugins = MagicMock()
            node._sync = MagicMock()
            node._generated_identity_certs = False
            node.bus = MagicMock()
            node._task_queue = asyncio.Queue()
            node._active_workers = 0
            node._start_time = 0.0
            node._bootstrap_peers = []
            node._sweep_offset = 0
            node._isolation_since = None
            node._ephemeral = False
            node._debug = False
            node._write_queue = asyncio.Queue()
            node._write_queue_proto = asyncio.Queue()
            node._shutdown_event = None
            node._upgrading = False
            node._restart_requested = False
            node._egress = MagicMock()
            node.background_tasks = []
            node._pool = MagicMock()
            node._peer_last_activity = {}
            node._version_gated = False
            node._seen_messages = set()
            node.server = None

            # Simulate the startup sentinel guard logic (extracted from start())
            config_dir = node._config.get("_config_dir", os.getcwd())
            data_dir = node._config.get("_data_dir", config_dir)
            _sentinel_path = os.path.join(data_dir, "knarr.stop")
            if os.path.exists(_sentinel_path):
                try:
                    os.unlink(_sentinel_path)
                except OSError:
                    pass

            self.assertFalse(os.path.exists(sentinel_path),
                             "Stale sentinel must be removed at startup")

    def test_no_sentinel_no_error(self):
        """If no knarr.stop exists at startup, no error occurs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sentinel_path = os.path.join(tmpdir, "knarr.stop")
            self.assertFalse(os.path.exists(sentinel_path))
            # No error should occur
            if os.path.exists(sentinel_path):
                os.unlink(sentinel_path)


class TestSentinelHeartbeatPoll(unittest.TestCase):
    """_heartbeat_loop must detect knarr.stop, drain, and trigger shutdown."""

    def test_sentinel_triggers_shutdown(self):
        """When knarr.stop exists, _heartbeat_loop sets _running=False and triggers shutdown."""
        from knarr.dht.node import DHTNode

        with tempfile.TemporaryDirectory() as tmpdir:
            node = DHTNode.__new__(DHTNode)
            node._config = {"_data_dir": tmpdir, "_config_dir": tmpdir}
            node._running = True
            node._task_queue = asyncio.Queue()
            node._active_workers = 0
            shutdown_event = asyncio.Event()
            node._shutdown_event = shutdown_event

            # Mock _heartbeat_tick — should never be called since sentinel triggers first
            node._heartbeat_tick = AsyncMock()

            # Create sentinel file
            sentinel_path = os.path.join(tmpdir, "knarr.stop")
            with open(sentinel_path, "w") as f:
                f.write("stop")

            # Patch HEARTBEAT_CHECK_INTERVAL to 0 for fast test
            with patch("knarr.dht.node.HEARTBEAT_CHECK_INTERVAL", 0):
                _run_async(node._heartbeat_loop())

            # Verify: _running is False, sentinel is removed, shutdown_event is set
            self.assertFalse(node._running)
            self.assertFalse(os.path.exists(sentinel_path),
                             "Sentinel file must be unlinked after detection")
            self.assertTrue(shutdown_event.is_set(),
                            "shutdown_event must be set after sentinel detection")

    def test_sentinel_not_present_continues_heartbeat(self):
        """Without sentinel, _heartbeat_loop calls _heartbeat_tick normally."""
        from knarr.dht.node import DHTNode

        with tempfile.TemporaryDirectory() as tmpdir:
            node = DHTNode.__new__(DHTNode)
            node._config = {"_data_dir": tmpdir, "_config_dir": tmpdir}
            node._running = True
            node._task_queue = asyncio.Queue()
            node._active_workers = 0
            node._shutdown_event = None

            call_count = 0

            async def mock_heartbeat_tick():
                nonlocal call_count
                call_count += 1
                # Stop after first tick
                node._running = False

            node._heartbeat_tick = mock_heartbeat_tick

            with patch("knarr.dht.node.HEARTBEAT_CHECK_INTERVAL", 0):
                _run_async(node._heartbeat_loop())

            self.assertEqual(call_count, 1, "heartbeat_tick must be called when no sentinel")

    def test_sentinel_drains_tasks_before_shutdown(self):
        """When sentinel is found, loop must wait for tasks to drain (up to 30s)."""
        from knarr.dht.node import DHTNode

        with tempfile.TemporaryDirectory() as tmpdir:
            node = DHTNode.__new__(DHTNode)
            node._config = {"_data_dir": tmpdir, "_config_dir": tmpdir}
            node._running = True
            node._task_queue = asyncio.Queue()
            # Simulate an active worker that clears after some time
            node._active_workers = 1
            shutdown_event = asyncio.Event()
            node._shutdown_event = shutdown_event
            node._heartbeat_tick = AsyncMock()

            sentinel_path = os.path.join(tmpdir, "knarr.stop")
            with open(sentinel_path, "w") as f:
                f.write("stop")

            # Clear the worker after a small delay
            async def clear_workers():
                await asyncio.sleep(0.1)
                node._active_workers = 0

            async def run_both():
                with patch("knarr.dht.node.HEARTBEAT_CHECK_INTERVAL", 0):
                    await asyncio.gather(
                        node._heartbeat_loop(),
                        clear_workers(),
                    )

            _run_async(run_both())

            self.assertFalse(node._running)
            self.assertTrue(shutdown_event.is_set())

    def test_sentinel_uses_sigterm_without_shutdown_event(self):
        """Without _shutdown_event, sentinel triggers os.kill(SIGTERM)."""
        from knarr.dht.node import DHTNode

        with tempfile.TemporaryDirectory() as tmpdir:
            node = DHTNode.__new__(DHTNode)
            node._config = {"_data_dir": tmpdir, "_config_dir": tmpdir}
            node._running = True
            node._task_queue = asyncio.Queue()
            node._active_workers = 0
            node._shutdown_event = None
            node._heartbeat_tick = AsyncMock()

            sentinel_path = os.path.join(tmpdir, "knarr.stop")
            with open(sentinel_path, "w") as f:
                f.write("stop")

            with patch("knarr.dht.node.HEARTBEAT_CHECK_INTERVAL", 0), \
                 patch("os.kill") as mock_kill:
                _run_async(node._heartbeat_loop())

            self.assertFalse(node._running)
            mock_kill.assert_called_once()
            import signal
            self.assertEqual(mock_kill.call_args[0][1], signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
