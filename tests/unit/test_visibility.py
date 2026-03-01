import asyncio
import hashlib
import pytest
from knarr.dht.node import DHTNode
from knarr.core.messages import TaskRequest

@pytest.mark.asyncio
async def test_private_skill_not_announced():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    # Register private skill
    node._skill_visibility["private-skill"] = "private"
    
    # Mock _send_to_peer to track announces
    sent_msgs = []
    async def mock_send(peer, msg):
        sent_msgs.append(msg)
    node._send_to_peer = mock_send
    
    # Add a peer so announce has someone to send to
    from knarr.core.models import NodeInfo
    node.storage.upsert_peer(NodeInfo("p1", "127.0.0.1", 9999))
    
    try:
        await node.announce({
            "name": "private-skill", "version": "1.0.0", "description": "d", "tags": ["t"],
            "input_schema": {}, "output_schema": {}
        })
        
        # Verify no announce sent
        assert len(sent_msgs) == 0
        
        # Verify stored locally
        own = node.storage.get_own_skills()
        assert any(s.name == "private-skill" for s in own)
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_private_skill_callable_via_call_local():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    node.register_handler("private-skill", lambda d: {"res": "ok"})
    node._skill_visibility["private-skill"] = "private"
    
    try:
        res = await node.call_local("private-skill", {})
        assert res == {"res": "ok"}
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_whitelist_allowed_node_succeeds():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    # Consumer key and node_id
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    vk = sk.verify_key.encode()
    consumer_node_id = hashlib.sha256(vk).hexdigest()
    
    node.register_handler("locked", lambda d: {"res": "open"})
    node._skill_visibility["locked"] = "whitelist"
    node._skill_allowed_nodes["locked"] = [consumer_node_id]
    
    await node.announce({
        "name": "locked", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}
    })
    
    try:
        from knarr.core.messages import sign_message
        req = sign_message(TaskRequest(
            task_id="t_locked", requester_node_id=consumer_node_id,
            requester_host="127.0.0.1", requester_port=9999, skill_name="locked", input_data={}
        ), sk)
        
        resp = await node._process_message(req)
        assert resp.status == "completed"
        assert resp.output_data == {"res": "open"}
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_whitelist_denied_node_returns_access_denied():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    node.register_handler("locked", lambda d: {"res": "open"})
    node._skill_visibility["locked"] = "whitelist"
    node._skill_allowed_nodes["locked"] = ["some-other-node"]
    
    await node.announce({
        "name": "locked", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}
    })
    
    try:
        from nacl.signing import SigningKey
        sk = SigningKey.generate()
        from knarr.core.messages import sign_message
        req = sign_message(TaskRequest(
            task_id="t_locked", requester_node_id="intruder",
            requester_host="127.0.0.1", requester_port=9999, skill_name="locked", input_data={}
        ), sk)
        
        resp = await node._process_message(req)
        assert resp.status == "failed"
        assert resp.error["code"] == "ACCESS_DENIED"
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_public_skill_announced():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    sent_msgs = []
    async def mock_send(peer, msg):
        sent_msgs.append(msg)
    node._send_to_peer = mock_send
    
    from knarr.core.models import NodeInfo
    node.storage.upsert_peer(NodeInfo("p1", "127.0.0.1", 9999))
    
    try:
        await node.announce({
            "name": "public-skill", "version": "1.0.0", "description": "d", "tags": ["t"],
            "input_schema": {}, "output_schema": {}
        })
        
        # Give event loop time to run background mock_send tasks
        await asyncio.sleep(0.1)
        
        assert len(sent_msgs) == 1
        assert sent_msgs[0].skill_key == "public-skill"
        
    finally:
        await node.stop()
