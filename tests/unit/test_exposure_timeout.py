import asyncio
import pytest
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch
from knarr.dashboard.server import CockpitServer


def _make_mock_node():
    """Build a mock node with all attributes the CockpitServer needs."""
    mock_node = MagicMock()
    mock_node._handlers = {"test-skill": (None, False)}
    mock_node.node_info.node_id = "mock" * 16
    mock_node.node_info.port = 9000
    mock_node.call_local = AsyncMock(return_value={"result": "ok"})
    mock_node._base_storage = mock_node.storage
    mock_node._base_bus = None
    mock_node._base_signing_key = None
    mock_node._base_public_key_hex = ""
    return mock_node


@pytest.mark.asyncio
async def test_exposure_timeout_config_respected():
    # _handle_exposure_execute now dispatches via _fire_and_forget_task for
    # local skills, which calls call_local with timeout_ms.
    mock_node = _make_mock_node()

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
    # Give the fire-and-forget background task a chance to run
    await asyncio.sleep(0.05)

    # Verify call_local was called with timeout_ms=5000
    mock_node.call_local.assert_called_once()
    _, kwargs = mock_node.call_local.call_args
    assert kwargs["timeout_ms"] == 5000


@pytest.mark.asyncio
async def test_exposure_timeout_default_30s():
    # Default timeout is 30s = 30000ms.
    mock_node = _make_mock_node()

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
    # Give the fire-and-forget background task a chance to run
    await asyncio.sleep(0.05)

    # Verify call_local was called with timeout_ms=30000
    mock_node.call_local.assert_called_once()
    _, kwargs = mock_node.call_local.call_args
    assert kwargs["timeout_ms"] == 30000
