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
    """V21-004: System mail handlers must reject status updates from non-provider senders.

    Logic lives in MailHandlers._handle_task_result_mail (moved from DHTNode in v0.43.0 C1).
    """
    from knarr.dht.mail_handlers import MailHandlers

    provider_id = "a" * 64
    attacker_id = "b" * 64
    job_id = "job-test-001"

    mock_storage = MagicMock()
    mock_storage.get_async_job.return_value = {
        "job_id": job_id,
        "provider_node_id": provider_id,
        "status": "remote",
    }
    mock_sidecar = MagicMock()
    mock_sidecar._signing_key = None

    handlers = MailHandlers(
        storage=mock_storage, bus=MagicMock(), asset_dir="", sidecar=mock_sidecar
    )
    handlers._enqueue_write = AsyncMock()

    # Call with attacker as sender — _enqueue_write must NOT be called
    item = {
        "from_node": attacker_id,
        "body": {"job_id": job_id, "output_data": {"result": "pwned"}},
    }
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(handlers._handle_task_result_mail(item))
    finally:
        loop.close()

    handlers._enqueue_write.assert_not_called()

    # Call with correct sender — _enqueue_write must be called
    item["from_node"] = provider_id
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(handlers._handle_task_result_mail(item))
    finally:
        loop.close()

    handlers._enqueue_write.assert_called_once()
