import asyncio
import pytest
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch
from knarr.dashboard.server import CockpitServer

@pytest.mark.asyncio
async def test_exposure_timeout_config_respected():
    # v0.17.0: cockpit uses submit_async_task, not call_local
    mock_node = MagicMock()
    mock_node._handlers = {"test-skill": (None, False)}
    mock_result = SimpleNamespace(status="completed", task_id="test-task-id")
    mock_node.submit_async_task = AsyncMock(return_value=mock_result)

    exposures = {
        "test-skill": {
            "skill": "test-skill",
            "timeout": 5
        }
    }
    server = CockpitServer(mock_node, exposures=exposures)

    mock_writer = MagicMock()
    body = b"{}"
    exposure = server._exposures["test-skill"]

    await server._handle_exposure_execute(mock_writer, body, exposure)

    # Verify submit_async_task was called with timeout_ms=5000
    mock_node.submit_async_task.assert_called_once()
    _, kwargs = mock_node.submit_async_task.call_args
    assert kwargs["timeout_ms"] == 5000

@pytest.mark.asyncio
async def test_exposure_timeout_default_30s():
    # v0.17.0: cockpit uses submit_async_task, not call_local
    mock_node = MagicMock()
    mock_node._handlers = {"test-skill": (None, False)}
    mock_result = SimpleNamespace(status="completed", task_id="test-task-id")
    mock_node.submit_async_task = AsyncMock(return_value=mock_result)

    exposures = {
        "test-skill": {
            "skill": "test-skill"
        }
    }
    server = CockpitServer(mock_node, exposures=exposures)

    mock_writer = MagicMock()
    body = b"{}"
    exposure = server._exposures["test-skill"]

    await server._handle_exposure_execute(mock_writer, body, exposure)

    # Verify submit_async_task was called with timeout_ms=30000
    mock_node.submit_async_task.assert_called_once()
    _, kwargs = mock_node.submit_async_task.call_args
    assert kwargs["timeout_ms"] == 30000
