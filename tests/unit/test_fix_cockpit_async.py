import asyncio
import pytest
import json
import uuid
from unittest.mock import MagicMock, AsyncMock, patch
from knarr.dashboard.server import CockpitServer

@pytest.mark.asyncio
async def test_cockpit_execute_returns_accepted():
    mock_node = MagicMock()
    mock_node._handlers = {"test-skill": (lambda x: x, False)}
    mock_node.node_info.node_id = "node1"
    mock_node.node_info.port = 9000
    mock_node.call_local = AsyncMock(return_value={"result": "ok"})
    mock_node._base_storage = mock_node.storage
    mock_node._base_bus = None
    mock_node._base_signing_key = None
    mock_node._base_public_key_hex = ""

    server = CockpitServer(mock_node)

    # Mock writer and respond methods
    mock_writer = MagicMock()
    server._respond = MagicMock()

    body = json.dumps({"skill": "test-skill", "input": {}, "local": True}).encode("utf-8")
    await server._handle_api_execute(mock_writer, body)

    # Verify it responded with 202 Accepted
    server._respond.assert_called_once()
    args = server._respond.call_args[0]
    assert args[1] == "202 Accepted"
    resp_body = json.loads(args[3].decode("utf-8"))
    # _fire_and_forget_task generates a UUID job_id
    assert "job_id" in resp_body
    # Verify it's a valid UUID
    uuid.UUID(resp_body["job_id"])
