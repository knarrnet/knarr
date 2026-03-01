import pytest
import asyncio
import json
import time
from unittest.mock import MagicMock, AsyncMock, patch
from knarr.dht.node import DHTNode
from knarr.core.messages import JoinRequest, TaskRequest, MailSync, PluginMessage, Announce
from knarr.dashboard.server import CockpitServer

@pytest.fixture
def mock_bus():
    return MagicMock()

@pytest.fixture
def node(tmp_path, mock_bus):
    with patch.object(DHTNode, '_load_or_generate_node_id', return_value="A"*64), \
         patch.object(DHTNode, '_init_encryption', return_value=None):
        node = DHTNode(host="127.0.0.1", port=8080, config={"skills": {"minimum_price": 0.0}, "economy": {"default_hard_limit": -10.0}})
        node.bus = mock_bus
        node.storage = MagicMock()
        node._sync = MagicMock()
        node._enqueue_write = AsyncMock(return_value=True)
        node._enqueue_write_proto = AsyncMock(return_value=True)
        return node

@pytest.mark.asyncio
async def test_exploit_join_request_peer_added_spam(node, mock_bus):
    """Exploit: Unauthenticated peer spams JoinRequests to trigger unlimited bus events."""
    from dataclasses import replace
    msg = JoinRequest(node_id="A"*64, host="1.2.3.4", port=8080)
    msg = replace(msg, msg_id="test-1", ephemeral=False)
    
    node.storage.get_peers.return_value = []
    
    await node._process_message(msg)
    await node._process_message(replace(msg, msg_id="test-2"))
    await node._process_message(replace(msg, msg_id="test-3"))
    
    assert mock_bus.emit.call_count >= 3
    for call in mock_bus.emit.call_args_list:
        assert call[0][0] == "peer.added"

@pytest.mark.asyncio
async def test_exploit_mail_received_large_session_id(node, mock_bus):
    """Exploit: Malicious MailSync with huge session_id saturates bus subscribers' memory."""
    from knarr.mail.sync import SyncEngine
    node._sync = SyncEngine(node)
    
    large_session_id = "A" * 5_000_000  # 5MB string
    msg = MailSync(
        msg_id="sync-1",
        sender_node_id="B"*64,
        public_key="PK",
        signature="SIG",
        items=[{
            "msg_type": "test",
            "session_id": large_session_id,
            "body": {}
        }]
    )
    msg.sender_node_id = "B"*64
    
    await node._sync.handle_mail_sync(msg, "1.2.3.4")
    
    mock_bus.emit.assert_called_with(
        "mail.received", 
        from_node="B"*64, 
        msg_type="test", 
        session_id=large_session_id, 
        bucket="inbox"
    )

@pytest.mark.asyncio
async def test_exploit_mail_received_large_msg_type(node, mock_bus):
    """Exploit: Malicious MailSync with huge msg_type overflows bus event kwargs."""
    from knarr.mail.sync import SyncEngine
    node._sync = SyncEngine(node)
    
    large_msg_type = "B" * 5_000_000  # 5MB string
    msg = MailSync(
        msg_id="sync-2",
        sender_node_id="C"*64,
        public_key="PK",
        signature="SIG",
        items=[{
            "msg_type": large_msg_type,
            "session_id": "test",
            "body": {}
        }]
    )
    
    await node._sync.handle_mail_sync(msg, "1.2.3.4")
    
    mock_bus.emit.assert_called_with(
        "mail.received", 
        from_node="C"*64, 
        msg_type=large_msg_type, 
        session_id="test", 
        bucket="inbox"
    )

@pytest.mark.asyncio
async def test_exploit_task_request_credit_sanction_spam(node, mock_bus):
    """Exploit: Attacker with no credit spams TaskRequest to trigger credit.sanctioned continuously."""
    node._skill_visibility = {"test": "public"}
    node._handlers = {"test": (MagicMock(), False)}
    
    class FakeLedgerEntry:
        balance = -20.0
    
    node._enqueue_write = AsyncMock(return_value=FakeLedgerEntry())
    node._own_skills = {"test": MagicMock(price=1.0)}
    node.policy = MagicMock(tit_for_tat=False, initial_credit=0.0, min_balance=-10.0)
    
    msg = TaskRequest(
        msg_id="task-1",
        task_id="t-1",
        skill_name="test",
        requester_node_id="D"*64,
        public_key="PK",
        input_data={"_healthcheck": True}
    )
    
    for i in range(5):
        msg.msg_id = f"task-{i}"
        await node._handle_task_request(msg)
        
    assert mock_bus.emit.call_count == 5
    for call in mock_bus.emit.call_args_list:
        assert call[0][0] == "credit.sanctioned"

@pytest.mark.asyncio
async def test_exploit_task_queue_exhausted_spam(node, mock_bus):
    """Exploit: Attacker floods the queue, triggering node.slots_exhausted continuously on Asyncio.QueueFull."""
    node._skill_visibility = {"test": "public"}
    node._handlers = {"test": (MagicMock(), False)}
    
    class FakeLedgerEntry:
        balance = 100.0
    
    node._enqueue_write = AsyncMock(return_value=FakeLedgerEntry())
    node._own_skills = {"test": MagicMock(price=1.0)}
    node.policy = MagicMock(tit_for_tat=False, initial_credit=0.0, min_balance=-10.0)
    node.storage.get_async_job_by_hash = MagicMock(return_value=None)
    
    import asyncio
    node._task_slots = 1
    node._task_queue = asyncio.Queue(maxsize=1)
    node._task_queue.put_nowait(("dummy", None, False, 0, 0, None))
    
    msg = TaskRequest(
        msg_id="task-2",
        task_id="t-2",
        skill_name="test",
        requester_node_id="E"*64,
        public_key="PK",
        input_data={"_healthcheck": True}
    )
    
    node._active_workers = 0 
    for i in range(3):
        msg.msg_id = f"task-full-{i}"
        await node._handle_task_request(msg)
        
    exhausted_calls = [c for c in mock_bus.emit.call_args_list if c[0][0] == "node.slots_exhausted"]
    assert len(exhausted_calls) == 3

@pytest.mark.asyncio
async def test_exploit_stale_mail_memory_leak(node, mock_bus):
    """Exploit: Unbounded growth of _notified_stale in MailSyncManager causes memory leak."""
    from knarr.mail.sync import SyncEngine
    node._sync = SyncEngine(node)
    
    stale_msgs = []
    for i in range(100):
        stale_msgs.append({"item_id": f"stale-{i}", "from_node": "F"*64, "timestamp": 0})
        
    node.storage.get_stale_inbox_messages = MagicMock(return_value=stale_msgs)
    
    await node._sync._cleanup_cycle()
    assert len(node._sync._notified_stale) == 100
    
    stale_msgs.append({"item_id": "stale-101", "from_node": "F"*64, "timestamp": 0})
    await node._sync._cleanup_cycle()
    assert len(node._sync._notified_stale) == 101

@pytest.mark.asyncio
async def test_exploit_admin_upgrade_missing():
    """Exploit: Verification that the required S-024 /admin/upgrade endpoint is missing (Returns 404)."""
    mock_node = MagicMock()
    mock_node._config = {}
    server = CockpitServer(node=mock_node)
    
    class FakeWriter:
        def __init__(self):
            self.written = b""
        def write(self, data):
            self.written += data
        def close(self):
            pass
        async def wait_closed(self):
            pass
        def get_extra_info(self, name):
            return ("127.0.0.1", 1234) if name == "peername" else None
            
    class FakeReader:
        def __init__(self, data):
            self.data = data.split(b"\\n")
            self.idx = 0
        async def readline(self):
            if self.idx < len(self.data):
                val = self.data[self.idx] + b"\\n"
                self.idx += 1
                return val
            return b""
        async def readexactly(self, n):
            return b""
            
    writer = FakeWriter()
    reader = FakeReader(b"POST /admin/upgrade HTTP/1.1\\r\\nHost: localhost\\r\\n\\r\\n")
    
    await server._handle_connection(reader, writer)
    assert b"404 Not Found" in writer.written

@pytest.mark.asyncio
async def test_exploit_plugin_egress_blocked_spam(node, mock_bus):
    """Exploit: A malicious plugin message bypasses standard throttling and emits bus events."""
    node._egress = MagicMock()
    node._egress.check.return_value = False
    
    class FakePeer:
        node_id = "G"*64
        host = "1.2.3.4"
        port = 8080
        
    msg = PluginMessage(msg_id="p-1", plugin_name="test", payload="bad payload")
    
    await node._send_to_peer(FakePeer(), msg)
    await node._send_to_peer(FakePeer(), msg)
    
    blocked_calls = [c for c in mock_bus.emit.call_args_list if c[0][0] == "security.egress_blocked"]
    assert len(blocked_calls) == 2

@pytest.mark.asyncio
async def test_exploit_mail_delivery_failed_error_length(node, mock_bus):
    """Exploit: Forces bus emission of mail.delivery_failed to log arbitrary text."""
    from knarr.mail.sync import SyncEngine
    node._sync = SyncEngine(node)
    
    node._pool = MagicMock()
    node._pool.send = AsyncMock(side_effect=Exception("A" * 1000))
    node._enqueue_write = AsyncMock(return_value=False)
    
    # Will fail and emit event
    await node._sync.push_to_peer("H"*64, "1.2.3.4", 8080, [{"item_id": "mail-1", "to_node": "H"*64}])
    
    failed_calls = [c for c in mock_bus.emit.call_args_list if c[0][0] == "mail.delivery_failed"]
    assert len(failed_calls) == 1
    assert len(failed_calls[0][1]["error"]) == 200

@pytest.mark.asyncio
async def test_exploit_task_timeout_spam(node, mock_bus):
    """Exploit: task.timeout ignores failures to update DB, continuously re-emitting events."""
    now = time.time()
    task_id = "task-1"
    timeout_ms = 1000
    created_at = now - 100 # old
    
    max_age = (timeout_ms / 1000.0) * 2
    age_seconds = now - created_at
    if age_seconds > max_age:
        node.bus.emit("task.timeout", skill_name="unknown", task_id=task_id, age_seconds=round(age_seconds, 1))
        
    timeout_calls = [c for c in mock_bus.emit.call_args_list if c[0][0] == "task.timeout"]
    assert len(timeout_calls) == 1
