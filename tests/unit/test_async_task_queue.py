import asyncio
import pytest
import json
import uuid
import sys
import hashlib
from dataclasses import replace
from unittest.mock import patch, MagicMock, AsyncMock
from knarr.dht.node import DHTNode
from knarr.core.messages import TaskRequest, TaskStatus, TaskResult

# Mock knarr.mail.tls so DHTNode can import without live TLS bindings.
# Cleaned up after this module's tests via _cleanup_tls_mock to prevent
# sys.modules contamination of test_tls.py and any other TLS-importing tests.
mock_tls = MagicMock()
mock_tls.resolve_cert_paths = MagicMock(return_value=("cert.pem", "key.pem"))
sys.modules["knarr.mail.tls"] = mock_tls

import pytest as _pytest

@_pytest.fixture(autouse=True, scope="module")
def _cleanup_tls_mock():
    """Remove the knarr.mail.tls mock from sys.modules after this module's tests."""
    yield
    sys.modules.pop("knarr.mail.tls", None)

@pytest.mark.asyncio
async def test_async_mode_returns_accepted():
    node = DHTNode("127.0.0.1", 0)
    node.register_handler("test", lambda d: {"ok": True})
    await node.start()
    await node.announce({"name": "test", "version": "1.0.0", "description": "test", "tags": ["test"], "input_schema": {}, "output_schema": {}})
    
    try:
        req = node._sign(TaskRequest(
            task_id="t1",
            skill_name="test",
            requester_node_id="req",
            requester_host="127.0.0.1",
            requester_port=9999,
            mode="async"
        ))
        
        # Simulate receiving request
        resp = await node._handle_task_request(req)
        
        assert isinstance(resp, TaskStatus)
        assert resp.status == "accepted"
        assert resp.task_id is not None
        assert resp.task_id != "t1" # Should be generated UUID
        
        # Verify job in storage
        job = node.storage.get_async_job(resp.task_id)
        assert job is not None
        assert job["status"] == "queued"
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_dedup_same_request_returns_existing():
    node = DHTNode("127.0.0.1", 0)
    node.register_handler("test", lambda d: {"ok": True})
    await node.start()
    await node.announce({"name": "test", "version": "1.0.0", "description": "test", "tags": ["test"], "input_schema": {}, "output_schema": {}})
    
    try:
        req1 = node._sign(TaskRequest(
            task_id="t1",
            skill_name="test",
            requester_node_id="req",
            requester_host="127.0.0.1",
            requester_port=9999,
            input_data={"x": 1},
            mode="async"
        ))
        
        resp1 = await node._handle_task_request(req1)
        assert resp1.status == "accepted"
        job_id = resp1.task_id
        
        # Second request with same skill + input + requester
        req2 = node._sign(TaskRequest(
            task_id="t2",
            skill_name="test",
            requester_node_id="req",
            requester_host="127.0.0.1",
            requester_port=9999,
            input_data={"x": 1},
            mode="async"
        ))
        
        resp2 = await node._handle_task_request(req2)
        assert isinstance(resp2, TaskStatus)
        assert resp2.task_id == job_id
        assert resp2.status in ("queued", "running")  # V013-008: worker may have transitioned to running
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_async_result_via_mail():
    # Enable mail in config
    config = {"mail": {"enabled": True}}
    node = DHTNode("127.0.0.1", 0, config=config)
    # We need knarr-mail to be registered
    # It is registered in DHTNode.start()
    
    node.register_handler("test", lambda d: {"ok": True})
    await node.start()
    await node.register_system_skills(config)
    await node.announce({"name": "test", "version": "1.0.0", "description": "test", "tags": ["test"], "input_schema": {}, "output_schema": {}})
    
    # Use external public_key so caller_node_id != self.node_info.node_id.
    # node.py:763 skips mail enqueue for self-calls (SELF_DELIVERY_SKIP);
    # an external caller forces the mail path. _handle_task_request does not
    # verify signatures, so replacing public_key after signing is safe.
    ext_pub_key = "bb" * 32  # 64-char hex external caller key
    ext_caller_node_id = hashlib.sha256(bytes.fromhex(ext_pub_key)).hexdigest()

    try:
        # Mock call_local("knarr-mail", ...) to verify it's called
        original_call_local = node.call_local
        node.call_local = AsyncMock(side_effect=original_call_local)

        req = replace(node._sign(TaskRequest(
            task_id="t1",
            skill_name="test",
            requester_node_id="req",
            requester_host="127.0.0.1",
            requester_port=9999,
            mode="async"
        )), public_key=ext_pub_key)

        resp = await node._handle_task_request(req)
        assert isinstance(resp, TaskStatus)
        job_id = resp.task_id

        # Wait for worker to finish task
        await asyncio.sleep(0.5)

        # Verify job status in storage
        job = node.storage.get_async_job(job_id)
        assert job["status"] == "completed"
        assert job["result"] == {"ok": True}

        # Verify mail was enqueued in outbox for the external caller
        outbox = node.storage.get_pending_outbox(ext_caller_node_id, limit=10)
        found_mail = False
        for item in outbox:
            body = json.loads(item["body_json"])
            if body.get("msg_type") == "knarr/system/task_result":
                if body["body"].get("job_id") == job_id:
                    found_mail = True
                    break
        assert found_mail
        
    finally:
        node.call_local = original_call_local
        await node.stop()
