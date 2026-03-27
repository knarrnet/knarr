"""Tests for v0.41.0 A2: Tick Starvation — Background Task Extraction."""
import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class FakeSync:
    """Minimal SyncEngine stand-in for testing."""
    def __init__(self, flush_delay=0):
        self._flush_delay = flush_delay
        self.flush_called = asyncio.Event()
        self.pull_called = asyncio.Event()
        self.cleanup_called = asyncio.Event()

    async def flush_outbox(self):
        if self._flush_delay > 0:
            await asyncio.sleep(self._flush_delay)
        self.flush_called.set()

    async def pull_from_correspondents(self):
        self.pull_called.set()

    async def cleanup(self):
        self.cleanup_called.set()


class FakeStorage:
    """Minimal Storage stand-in."""
    def __init__(self):
        self._peers = []

    def get_peers(self):
        return self._peers

    async def cleanup_expired_jobs(self):
        pass


class FakePool:
    async def evict_idle(self, timeout):
        pass


class FakePlugins:
    async def on_tick(self, peers, health):
        pass

    async def on_shutdown(self):
        pass


class FakeBus:
    def tick(self):
        return 0

    def emit(self, *args, **kwargs):
        pass


class FakeNode:
    """Minimal DHTNode stand-in for testing extracted background tasks."""
    def __init__(self, config=None):
        self._running = True
        self._config = config or {"node": {}, "mail": {}}
        self._sync = FakeSync()
        self.storage = FakeStorage()
        self._plugins = FakePlugins()
        self._pool = FakePool()
        self.bus = FakeBus()
        self._active_connections = 0
        self._write_queue = asyncio.Queue()
        self._start_time = time.monotonic()
        self._connection_idle_timeout = 300
        self._bootstrap_peers = []
        self._peer_last_activity = {}
        self._peer_dead_timeout = 300
        self._heartbeat_silence_threshold = 90
        self.node_info = MagicMock()
        self.node_info.node_id = "test_node_id"
        self.background_tasks = []

    _protocol_pool = None
    _handler_pool = None

    async def _run_in_protocol_pool(self, fn, *args):
        return fn(*args)

    async def _enqueue_write(self, fn, *args, **kwargs):
        if asyncio.iscoroutinefunction(fn):
            return await fn(*args, **kwargs)
        return fn(*args, **kwargs)

    async def _run_netting_cycle_if_due(self):
        pass

    async def join(self, peers):
        pass


@pytest.mark.asyncio
async def test_on_tick_completes_fast_even_when_flush_slow():
    """on_tick completes in <1s even when flush_outbox would be slow."""
    from knarr.dht.node import DHTNode

    node = FakeNode()
    node._sync = FakeSync(flush_delay=10)  # 10s delay on flush

    # Import the patched _heartbeat_tick
    # The tick should NOT call flush_outbox anymore
    tick_fn = DHTNode._heartbeat_tick

    start = time.monotonic()
    await tick_fn(node)
    elapsed = time.monotonic() - start

    # Tick should complete fast — no flush_outbox call
    assert elapsed < 2.0, f"Tick took {elapsed:.1f}s — flush_outbox may still be inline"


@pytest.mark.asyncio
async def test_background_flush_runs_independently():
    """flush_outbox runs on its own interval via background loop."""
    from knarr.dht.node import DHTNode

    node = FakeNode()
    node._config = {"node": {"flush_interval": 0.1}, "mail": {}}

    task = asyncio.create_task(DHTNode._flush_outbox_loop(node))
    try:
        await asyncio.wait_for(node._sync.flush_called.wait(), timeout=2.0)
        assert node._sync.flush_called.is_set()
    finally:
        node._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_background_pull_runs_independently():
    """pull_from_correspondents runs on its own interval."""
    from knarr.dht.node import DHTNode

    node = FakeNode()
    node._config = {"node": {}, "mail": {"pull_interval": 0.1}}

    task = asyncio.create_task(DHTNode._pull_from_correspondents_loop(node))
    try:
        await asyncio.wait_for(node._sync.pull_called.wait(), timeout=2.0)
        assert node._sync.pull_called.is_set()
    finally:
        node._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_shutdown_cancels_background_tasks():
    """All background tasks are cancelled during shutdown."""
    from knarr.dht.node import DHTNode

    node = FakeNode()
    node._config = {"node": {"flush_interval": 0.1, "sweep_interval": 0.1},
                     "mail": {"pull_interval": 0.1}}

    # Start all three background tasks
    tasks = [
        asyncio.create_task(DHTNode._flush_outbox_loop(node)),
        asyncio.create_task(DHTNode._pull_from_correspondents_loop(node)),
        asyncio.create_task(DHTNode._peer_heartbeat_sweep_loop(node)),
    ]
    node.background_tasks = tasks

    # Let them run briefly
    await asyncio.sleep(0.2)

    # Shutdown: cancel all
    node._running = False
    for task in node.background_tasks:
        task.cancel()

    # Wait for all to finish — should not hang
    done, pending = await asyncio.wait(tasks, timeout=2.0)
    assert len(pending) == 0, f"Dangling tasks: {pending}"


@pytest.mark.asyncio
async def test_failed_background_op_logs_warning(caplog):
    """Failed background operation logs warning but does not crash."""
    from knarr.dht.node import DHTNode

    node = FakeNode()
    node._config = {"node": {"flush_interval": 1.0}, "mail": {}}

    # Make flush_outbox raise an exception
    async def failing_flush():
        raise RuntimeError("network error")

    node._sync.flush_outbox = failing_flush
    node._sync.flush_called = asyncio.Event()  # won't be set

    task = asyncio.create_task(DHTNode._flush_outbox_loop(node))

    # Let it run one cycle (interval clamped to min 1.0s)
    await asyncio.sleep(1.3)

    node._running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Should have logged warning, not crashed
    assert any("FLUSH_OUTBOX_FAIL" in r.message for r in caplog.records), \
        "Expected FLUSH_OUTBOX_FAIL warning in logs"


@pytest.mark.asyncio
async def test_heartbeat_tick_has_no_network_io():
    """_heartbeat_tick should not call flush_outbox, pull, or peer sweep."""
    from knarr.dht.node import DHTNode

    node = FakeNode()

    # Track if network I/O methods are called
    flush_called = False
    pull_called = False
    sweep_called = False

    original_flush = node._sync.flush_outbox
    original_pull = node._sync.pull_from_correspondents

    async def track_flush():
        nonlocal flush_called
        flush_called = True
        await original_flush()

    async def track_pull():
        nonlocal pull_called
        pull_called = True
        await original_pull()

    node._sync.flush_outbox = track_flush
    node._sync.pull_from_correspondents = track_pull

    await DHTNode._heartbeat_tick(node)

    assert not flush_called, "flush_outbox should NOT be called in _heartbeat_tick"
    assert not pull_called, "pull_from_correspondents should NOT be called in _heartbeat_tick"
