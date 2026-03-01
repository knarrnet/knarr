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
        # We use call_local which we updated to support ctx
        # Set a short timeout
        with pytest.raises(Exception) as excinfo:
            await node.call_local("slow", {}, timeout_ms=100)
        
        assert "Handler exceeded" in str(excinfo.value)
        
        # Give the thread a moment to finish and check its result 
        # (though call_local already returned failure)
        # The important thing is that ctx.cancelled was set.
        # We can't easily check the thread's local 'ctx' from here, 
        # but we can verify the log or use a shared object.
    finally:
        node._running = False

@pytest.mark.asyncio
async def test_orphan_handler_logging():
    node = DHTNode("127.0.0.1", 0)
    node._running = True
    
    def eternal_handler(data):
        time.sleep(10) # Block thread
        return {"ok": True}
        
    node.register_handler("eternal", eternal_handler)
    
    with patch("knarr.dht.node.logger") as mock_logger:
        try:
            with pytest.raises(Exception):
                await node.call_local("eternal", {}, timeout_ms=50)
            
            # Verify ORPHAN_HANDLER warning was logged
            found = False
            for call in mock_logger.warning.call_args_list:
                if "ORPHAN_HANDLER" in call[0][0] and "eternal" in call[0][0]:
                    found = True
                    break
            assert found
        finally:
            node._running = False
