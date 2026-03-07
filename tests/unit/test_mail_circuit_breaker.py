import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from knarr.core.models import NodeInfo
from knarr.mail.sync import SyncEngine


def _make_node():
    node = MagicMock()
    node.node_info = NodeInfo(node_id="a" * 64, host="127.0.0.1", port=9030)
    node._config = {"mail": {"debug": False}}
    node._egress = MagicMock()
    node._egress.check.return_value = True
    node.storage = MagicMock()
    node.storage.get_peer_encryption_key.return_value = None
    node.storage.mark_outbox_sending = MagicMock()
    node.storage.mark_outbox_pending = MagicMock()
    node.storage.mark_outbox_delivered_for_peer = MagicMock()
    node._enqueue_write = AsyncMock(return_value=None)
    node._sign = lambda msg: msg
    node.resolve_peer = lambda node_id, host, port: (host, port)
    node._pool = MagicMock()
    node.bus = MagicMock()
    return node


@pytest.mark.asyncio
async def test_circuit_open_skips_delivery_until_retry_time():
    node = _make_node()
    node.storage.get_pending_outbox.return_value = []
    sync = SyncEngine(node)
    peer_id = "b" * 64
    sync._peer_delivery_state[peer_id] = {
        "last_attempt": time.time(),
        "consecutive_failures": 5,
        "next_retry_after": time.time() + 60,
        "circuit_open": True,
    }
    await sync._push_to_peer_inner(peer_id, "127.0.0.1", 9000)
    node.storage.get_pending_outbox.assert_not_called()


@pytest.mark.asyncio
async def test_backoff_skips_delivery_until_next_retry():
    node = _make_node()
    node.storage.get_pending_outbox.return_value = []
    sync = SyncEngine(node)
    peer_id = "b" * 64
    sync._peer_delivery_state[peer_id] = {
        "last_attempt": time.time(),
        "consecutive_failures": 2,
        "next_retry_after": time.time() + 60,
        "circuit_open": False,
    }
    await sync._push_to_peer_inner(peer_id, "127.0.0.1", 9000)
    node.storage.get_pending_outbox.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_failures_open_circuit():
    node = _make_node()
    node.storage.get_pending_outbox.return_value = [{
        "item_id": "item-1",
        "batch_seq": 1,
        "body_json": json.dumps({"item_id": "item-1", "body": {"ok": True}, "msg_type": "knarr/system/task_result"}),
    }]
    node._pool.send = AsyncMock(side_effect=RuntimeError("offline"))
    sync = SyncEngine(node)
    peer_id = "b" * 64

    for _ in range(5):
        await sync._push_to_peer_inner(peer_id, "127.0.0.1", 9000)
        sync._peer_delivery_state[peer_id]["next_retry_after"] = 0

    assert sync._peer_delivery_state[peer_id]["circuit_open"] is True
