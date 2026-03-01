"""Tests for private skill visibility enforcement."""
import time
import pytest
import nacl.signing
from knarr.dht.node import DHTNode
from knarr.core.messages import TaskRequest


def _foreign_key_hex() -> str:
    """Generate a foreign Ed25519 public key (not this node's)."""
    return nacl.signing.SigningKey.generate().verify_key.encode().hex()


@pytest.mark.asyncio
async def test_private_skill_blocks_remote_execution():
    """Bug fix: private skills must deny remote TaskRequest execution."""
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        async def echo(data):
            return data

        node.register_handler("secret-skill", echo)
        node._skill_visibility["secret-skill"] = "private"
        await node.announce({
            "name": "secret-skill", "version": "1.0.0",
            "description": "private", "tags": ["test"],
            "input_schema": {"text": "string"},
            "output_schema": {"text": "string"},
        })

        # Simulate a remote TaskRequest with a FOREIGN key
        foreign_pk = _foreign_key_hex()
        msg = TaskRequest(
            task_id="test-task-1",
            requester_node_id="remote-node-id",
            requester_host="127.0.0.1",
            requester_port=9999,
            skill_name="secret-skill",
            input_data={"text": "hello"},
            timeout_ms=5000,
            public_key=foreign_pk,
            signature="",
        )
        result = await node._handle_task_request(msg)
        assert result.status == "failed"
        assert result.error["code"] == "ACCESS_DENIED"
        assert "private" in result.error["message"].lower()
    finally:
        await node.stop()


@pytest.mark.asyncio
async def test_private_skill_allows_self_call_via_task_request():
    """Self-calls (cockpit slow dispatch) must bypass visibility for private skills."""
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        async def echo(data):
            return {"text": data["text"]}

        node.register_handler("secret-skill", echo)
        node._skill_visibility["secret-skill"] = "private"
        await node.announce({
            "name": "secret-skill", "version": "1.0.0",
            "description": "private", "tags": ["test"],
            "input_schema": {"text": "string"},
            "output_schema": {"text": "string"},
        })

        # Self-call: public_key matches the node's own key
        msg = TaskRequest(
            task_id="test-self-call-1",
            requester_node_id=node.node_info.node_id,
            requester_host="127.0.0.1",
            requester_port=node.node_info.port,
            skill_name="secret-skill",
            input_data={"text": "hello"},
            timeout_ms=5000,
            public_key=node._public_key_hex,
            signature="",
        )
        result = await node._handle_task_request(msg)
        # Should NOT be ACCESS_DENIED — self-calls bypass visibility
        assert result.status != "failed" or result.error.get("code") != "ACCESS_DENIED"
    finally:
        await node.stop()


@pytest.mark.asyncio
async def test_private_skill_still_callable_locally():
    """Private skills must remain callable via call_local."""
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        async def echo(data):
            return {"text": data["text"]}

        node.register_handler("secret-skill", echo)
        node._skill_visibility["secret-skill"] = "private"

        result = await node.call_local("secret-skill", {"text": "hello"})
        assert result == {"text": "hello"}
    finally:
        await node.stop()


@pytest.mark.asyncio
async def test_private_skill_not_in_query_results():
    """Private skills must not appear in QueryResponse."""
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        async def echo(data):
            return data

        node.register_handler("public-skill", echo)
        node._skill_visibility["public-skill"] = "public"
        await node.announce({
            "name": "public-skill", "version": "1.0.0",
            "description": "public", "tags": ["test"],
            "input_schema": {}, "output_schema": {},
        })

        node.register_handler("private-skill", echo)
        node._skill_visibility["private-skill"] = "private"
        await node.announce({
            "name": "private-skill", "version": "1.0.0",
            "description": "private", "tags": ["test"],
            "input_schema": {}, "output_schema": {},
        })

        # Query by name via the node's message processor — should filter private skills
        from knarr.core.messages import Query, QueryResponse
        msg = Query(query_type="name", value="private-skill", public_key=node._public_key_hex)
        response = await node._process_message(msg)
        assert isinstance(response, QueryResponse)
        assert len(response.results) == 0

        # Public skill should still be discoverable
        msg2 = Query(query_type="name", value="public-skill", public_key=node._public_key_hex)
        response2 = await node._process_message(msg2)
        assert isinstance(response2, QueryResponse)
        assert len(response2.results) > 0
    finally:
        await node.stop()


@pytest.mark.asyncio
async def test_visibility_change_public_to_private_deregisters():
    """Changing visibility from public to private should deregister from DHT."""
    from knarr.core.messages import Query, QueryResponse
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        async def echo(data):
            return data

        # Register as public first
        node._skill_visibility["my-skill"] = "public"
        node.register_handler("my-skill", echo)
        await node.announce({
            "name": "my-skill", "version": "1.0.0",
            "description": "test", "tags": ["test"],
            "input_schema": {}, "output_schema": {},
        })

        # Verify it's discoverable via query
        msg = Query(query_type="name", value="my-skill", public_key=node._public_key_hex)
        resp = await node._process_message(msg)
        assert isinstance(resp, QueryResponse)
        assert len(resp.results) > 0

        # Simulate visibility change to private (as SIGHUP reload would do)
        old_vis = node._skill_visibility.get("my-skill", "public")
        node._skill_visibility["my-skill"] = "private"
        if old_vis != "private":
            await node.deregister("my-skill")

        # Verify it's no longer discoverable
        resp2 = await node._process_message(msg)
        assert isinstance(resp2, QueryResponse)
        assert len(resp2.results) == 0

        # But handler is still registered for call_local
        assert "my-skill" in node._handlers
        result = await node.call_local("my-skill", {"text": "hello"})
        assert result["text"] == "hello"
    finally:
        await node.stop()
