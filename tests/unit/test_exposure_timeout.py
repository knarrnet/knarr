import asyncio
import pytest
import json
from unittest.mock import MagicMock, AsyncMock, patch
from knarr.dashboard.server import CockpitServer

@pytest.mark.asyncio
async def test_exposure_timeout_config_respected():
    mock_node = MagicMock()
    mock_node._handlers = {"test-skill": (None, False)}
    mock_node.call_local = AsyncMock(return_value={"ok": True})
    
    exposures = {
        "test-skill": {
            "skill": "test-skill",
            "timeout": 5
        }
    }
    server = CockpitServer(mock_node, exposures=exposures)
    
    # Simulate POST /s/test-skill/execute
    mock_writer = MagicMock()
    body = b"{}"
    exposure = server._exposures["test-skill"]
    
    await server._handle_exposure_execute(mock_writer, body, exposure)
    
    # Verify call_local was called with timeout_ms=5000
    mock_node.call_local.assert_called_once()
    args, kwargs = mock_node.call_local.call_args
    assert kwargs["timeout_ms"] == 5000

@pytest.mark.asyncio
async def test_exposure_timeout_default_30s():
    mock_node = MagicMock()
    mock_node._handlers = {"test-skill": (None, False)}
    mock_node.call_local = AsyncMock(return_value={"ok": True})
    
    exposures = {
        "test-skill": {
            "skill": "test-skill"
        }
    }
    server = CockpitServer(mock_node, exposures=exposures)
    
    # Simulate POST /s/test-skill/execute
    mock_writer = MagicMock()
    body = b"{}"
    exposure = server._exposures["test-skill"]
    
    await server._handle_exposure_execute(mock_writer, body, exposure)
    
    # Verify call_local was called with timeout_ms=30000
    mock_node.call_local.assert_called_once()
    args, kwargs = mock_node.call_local.call_args
    assert kwargs["timeout_ms"] == 30000
