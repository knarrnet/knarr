import asyncio
import pytest
import json
from unittest.mock import MagicMock, AsyncMock, patch
from knarr.dashboard.server import CockpitServer

@pytest.mark.asyncio
async def test_cockpit_execute_returns_accepted():
    mock_node = MagicMock()
    # Mock status response object
    status_resp = MagicMock()
    status_resp.task_id = "job123"
    status_resp.position = 1
    status_resp.status = "accepted"
    status_resp.reason = None
    
    mock_node.submit_async_task = AsyncMock(return_value=status_resp)
    mock_node._handlers = {"test-skill": (lambda x: x, False)}
    mock_node.node_info.node_id = "node1"
    mock_node.node_info.port = 9000
    
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
    assert resp_body["job_id"] == "job123"
