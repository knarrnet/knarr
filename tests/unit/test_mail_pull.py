"""Tests for Tier 2 Mail Pull mechanics."""
import pytest
import asyncio
import json
import time
from unittest.mock import MagicMock, AsyncMock

from knarr.core.messages import MailPullReq, MailPullResp, MailPullAck
from knarr.mail.sync import SyncEngine


@pytest.fixture
def mock_node():
    node = MagicMock()
    node._config = {"mail": {"pull_interval": 300, "max_pull_batch": 5}}
    node.node_info = MagicMock()
    node.node_info.node_id = "test_node_A"
    node.storage = MagicMock()
    node._sign = lambda x: x

    async def mock_enqueue(func, *args, **kwargs):
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        return func(*args, **kwargs)

    node._enqueue_write = AsyncMock(side_effect=mock_enqueue)
    return node


@pytest.mark.asyncio
async def test_pull_req_returns_pending_items(mock_node):
    engine = SyncEngine(mock_node)
    pending = [
        {"item_id": "i1", "to_node": "req1", "body_json": '{"foo": "bar"}', "batch_seq": 0},
        {"item_id": "i2", "to_node": "req1", "body_json": '{"baz": "qux"}', "batch_seq": 0}
    ]
    mock_node.storage.get_pending_outbox_for_requester.return_value = pending
    req = MailPullReq(requester_node_id="req1")
    resp = await engine.handle_mail_pull_req(req, "1.2.3.4")

    assert isinstance(resp, MailPullResp)
    assert len(resp.items) == 2
    assert resp.items[0]["foo"] == "bar"
    assert resp.items[1]["baz"] == "qux"


@pytest.mark.asyncio
async def test_pull_req_rate_limited(mock_node):
    engine = SyncEngine(mock_node)
    mock_node.storage.get_pending_outbox_for_requester.return_value = [
        {"item_id": "i1", "to_node": "req1", "body_json": '{"x": 1}', "batch_seq": 0}
    ]
    # First request
    req = MailPullReq(requester_node_id="req1")
    await engine.handle_mail_pull_req(req, "1.2.3.4")

    # Second request immediately — should be rate limited
    resp = await engine.handle_mail_pull_req(req, "1.2.3.4")
    assert isinstance(resp, MailPullResp)
    assert len(resp.items) == 0
    # Storage was called only once (from first request)
    mock_node.storage.get_pending_outbox_for_requester.assert_called_once()


@pytest.mark.asyncio
async def test_pull_ack_marks_delivered(mock_node):
    engine = SyncEngine(mock_node)
    ack = MailPullAck(requester_node_id="req1", item_ids=["i1", "i2"])
    await engine.handle_mail_pull_ack(ack)
    mock_node.storage.mark_outbox_pull_delivered.assert_called_once_with(["i1", "i2"], "req1")


@pytest.mark.asyncio
async def test_pull_storm_mitigation(mock_node):
    engine = SyncEngine(mock_node)
    corrs = [{"node_id": f"p{i}"} for i in range(10)]
    mock_node.storage.get_correspondents.return_value = corrs
    engine._pull_from_peer = AsyncMock(return_value=1)

    original_sleep = asyncio.sleep
    asyncio.sleep = AsyncMock()
    try:
        await engine.pull_from_correspondents()
    finally:
        asyncio.sleep = original_sleep

    # Verify only 5 peers were pulled from (storm mitigation)
    assert engine._pull_from_peer.call_count == 5


@pytest.mark.asyncio
async def test_correspondent_tracking_on_send(mock_node):
    engine = SyncEngine(mock_node)
    mock_node.storage.count_outbox.return_value = 0
    await engine.enqueue("to_peer_b", "chat", {"hello": "world"})
    mock_node.storage.upsert_correspondent.assert_called_with("to_peer_b", sent=True, received=False)


def test_sentinel_pull_address_binding():
    """Sentinel: pull only returns items TO requester."""
    import os
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    storage_path = os.path.join(base_dir, "src", "knarr", "dht", "storage.py")
    with open(storage_path, "r") as f:
        content = f.read()
    assert "WHERE to_node = ?" in content, "Sentinel failed: get_pending_outbox_for_requester must filter by to_node"
