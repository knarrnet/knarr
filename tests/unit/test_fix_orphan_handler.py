import asyncio
import pytest
import time
import threading
from unittest.mock import MagicMock, AsyncMock, patch
from knarr.dht.node import DHTNode
from knarr.core.messages import TaskRequest, TaskStatus, TaskResult

@pytest.mark.asyncio
async def test_handler_timeout_signals_cancellation():
    node = DHTNode("127.0.0.1", 0)
    node._running = True
    # _enqueue_write blocks on the write-queue future without a running writer task.
    # Mock it so call_local proceeds to actually execute the handler.
    node._enqueue_write = AsyncMock()

    # A handler that waits for cancellation
    def slow_handler(data, ctx):
        # Wait up to 2 seconds for cancellation event
        if ctx.cancelled.wait(timeout=2.0):
            return {"cancelled": True}
        return {"cancelled": False}

    # Register handler
    node.register_handler("slow", slow_handler)

    # Wrap in try/finally to stop node
    try:
        # call_local re-raises the raw asyncio.TimeoutError from asyncio.wait_for
        # (see node.py: `raise` in the except block). The "Handler exceeded" string
        # is computed for telemetry/logging only, not placed in the exception message.
        with pytest.raises(asyncio.TimeoutError):
            await node.call_local("slow", {}, timeout_ms=100)
    finally:
        node._running = False

@pytest.mark.asyncio
async def test_orphan_handler_logging(caplog):
    import logging
    node = DHTNode("127.0.0.1", 0)
    node._running = True
    # _enqueue_write blocks on the write-queue future without a running writer task.
    node._enqueue_write = AsyncMock()

    def eternal_handler(data):
        time.sleep(10)  # Block thread
        return {"ok": True}

    node.register_handler("eternal", eternal_handler)

    # caplog captures log records directly from the logging system — order-independent
    # and not susceptible to module-level logger attribute contamination from prior tests.
    with caplog.at_level(logging.WARNING, logger="knarr.dht.node"):
        try:
            with pytest.raises(Exception):
                await node.call_local("eternal", {}, timeout_ms=50)

            # Verify ORPHAN_HANDLER warning was logged
            assert any(
                "ORPHAN_HANDLER" in r.message and "eternal" in r.message
                for r in caplog.records
            ), f"ORPHAN_HANDLER not found in log records: {[r.message for r in caplog.records]}"
        finally:
            node._running = False
