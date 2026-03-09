import asyncio
import pytest
import json
import time
from unittest.mock import MagicMock, AsyncMock, patch
from knarr.mail.sync import SyncEngine
from knarr.core.messages import MailSync, MailAck, Ack

@pytest.fixture
def mock_node():
    node = MagicMock()
    node.node_info.node_id = "sender"
    node.storage = MagicMock()
    node.storage.get_peer_encryption_key.return_value = None
    node._pool = AsyncMock()
    node._sign = lambda m: m
    node._enqueue_write = AsyncMock(side_effect=lambda op, *args: op(*args))
    node.resolve_peer = MagicMock(side_effect=lambda nid, host, port: (host, port))
    # v0.41.0 B1: Admission pipeline support
    node._resolve_policy = MagicMock(return_value=(100.0, -50.0))
    node._get_initial_trust = MagicMock(return_value=1.0)
    ledger_entry = MagicMock()
    ledger_entry.balance = 100.0
    ledger_entry.prepaid = 0.0
    node.storage.get_or_create_ledger_entry = MagicMock(return_value=ledger_entry)
    node.storage.meter_get = MagicMock(return_value=None)
    node.storage.update_ledger_provider = MagicMock()
    node._group_engine = None
    node._own_skills = {}
    node._get_skill_runtime_config = MagicMock(return_value={})
    node._load_discount_rules = MagicMock(return_value=[])
    node._get_cost_projection = MagicMock(return_value=None)
    node._build_pricing_config = MagicMock(return_value={})
    node._config = {"mail": {"price": 1.0}}
    node.policy = MagicMock()
    node.policy.tit_for_tat = False
    node._maybe_send_tab_reminder = AsyncMock()
    node.bus = MagicMock()
    return node

@pytest.mark.asyncio
async def test_enqueue_mail(mock_node):
    engine = SyncEngine(mock_node)
    mock_node.storage.count_outbox.return_value = 0
    mock_node.storage.enqueue_outbox.return_value = 1
    
    item_id = await engine.enqueue("recipient", "knarr/user/text", {"hello": "world"})
    
    assert item_id is not None
    mock_node.storage.enqueue_outbox.assert_called_once()
    args = mock_node.storage.enqueue_outbox.call_args[0]
    assert args[0] == item_id
    assert args[1] == "recipient"
    body = json.loads(args[2])
    assert body["body"] == {"hello": "world"}

@pytest.mark.asyncio
async def test_push_to_peer(mock_node):
    engine = SyncEngine(mock_node)
    mock_node.storage.get_pending_outbox.return_value = [
        {"item_id": "i1", "batch_seq": 1, "body_json": json.dumps({"item_id": "i1", "body": {}})}
    ]
    mock_node._pool.send.return_value = MagicMock(spec=MailAck)
    
    await engine.push_to_peer("peer1", "1.2.3.4", 9000)
    
    mock_node.storage.mark_outbox_sending.assert_called_once_with(["i1"])
    mock_node._pool.send.assert_called_once()
    args = mock_node._pool.send.call_args[0]
    assert args[0] == "peer1"
    assert isinstance(args[3], MailSync)
    assert len(args[3].items) == 1

@pytest.mark.asyncio
async def test_handle_mail_sync(mock_node):
    engine = SyncEngine(mock_node)
    mock_node.storage.count_mail.return_value = 0
    mock_node.storage.count_mail_inbox.return_value = 0
    mock_node.storage.store_mail_from_sync.return_value = True

    # v0.41.0 B1: Mock admission pipeline for non-system mail
    mock_gate = MagicMock()
    mock_gate.outcome = "accept"
    mock_gate.balance_after = 99.0
    mock_pricing = MagicMock()
    mock_pricing.final_price = 1.0
    mock_result = MagicMock()
    mock_result.gate = mock_gate
    mock_result.pricing = mock_pricing

    msg = MailSync(sender_node_id="peer1", items=[
        {"item_id": "i1", "from_node": "peer1", "timestamp": 100, "body": {"hi": 1},
         "msg_type": "knarr/user/text", "ttl_expires": time.time() + 100}
    ], batch_seq=1)

    with patch("knarr.commerce.admission_pipeline.run_admission", return_value=mock_result) as mock_admit:
        resp = await engine.handle_mail_sync(msg, "1.2.3.4")

    assert isinstance(resp, MailAck)
    assert resp.item_ids == ["i1"]
    mock_node.storage.store_mail_from_sync.assert_called_once()

@pytest.mark.asyncio
async def test_handle_mail_ack(mock_node):
    engine = SyncEngine(mock_node)
    msg = MailAck(sender_node_id="peer1", ack_seq=1, item_ids=["i1", "i2"])
    
    await engine.handle_mail_ack(msg)
    
    mock_node.storage.mark_outbox_delivered_for_peer.assert_called_once_with(["i1", "i2"], "peer1")

@pytest.mark.asyncio
async def test_system_dispatch(mock_node):
    engine = SyncEngine(mock_node)
    handler = AsyncMock()
    engine.register_handler("knarr/system/test", handler)
    
    item = {"item_id": "i1", "msg_type": "knarr/system/test", "body": {"x": 1}}
    await engine._dispatch_system_item(item)
    
    handler.assert_called_once_with(item)
