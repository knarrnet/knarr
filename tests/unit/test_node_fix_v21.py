import asyncio
import threading
from unittest.mock import MagicMock, AsyncMock, patch
from knarr.dht.sidecar import TaskContext

def test_task_context_has_cancelled():
    """Verify TaskContext has a cancelled attribute that is a threading.Event, initially not set."""
    ctx = TaskContext(_asset_dir="test_assets")
    assert hasattr(ctx, "cancelled")
    assert isinstance(ctx.cancelled, threading.Event)
    assert not ctx.cancelled.is_set()


def test_system_mail_rejects_wrong_sender():
    """V21-004: System mail handlers must reject status updates from non-provider senders."""
    from knarr.dht.node import DHTNode

    # We need to test the handler logic without constructing a full node.
    # Extract the handler method and call it with a mock self.
    mock_node = MagicMock()
    mock_node.storage = MagicMock()

    # Simulate a remote job tracked with provider_node_id = "aaa..."
    provider_id = "a" * 64
    attacker_id = "b" * 64
    job_id = "job-test-001"

    mock_node.storage.get_async_job.return_value = {
        "job_id": job_id,
        "provider_node_id": provider_id,
        "status": "remote",
    }
    mock_node._enqueue_write = AsyncMock()

    # Call the handler with attacker as sender
    item = {
        "from_node": attacker_id,
        "body": {"job_id": job_id, "output_data": {"result": "pwned"}},
    }

    # Run the unbound method with our mock
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(DHTNode._handle_task_result_mail(mock_node, item))
    finally:
        loop.close()

    # _enqueue_write should NOT have been called — sender doesn't match provider
    mock_node._enqueue_write.assert_not_called()

    # Now test with correct sender — should go through
    item["from_node"] = provider_id
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(DHTNode._handle_task_result_mail(mock_node, item))
    finally:
        loop.close()

    mock_node._enqueue_write.assert_called_once()
