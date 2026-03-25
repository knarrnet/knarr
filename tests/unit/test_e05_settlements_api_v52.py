"""E-05: /api/settlements endpoint exists and returns structured data.

Verifies:
- The endpoint is wired in CockpitServer
- _handle_settlements_list delegates to storage.get_settlement_queue_page()
- Response shape: {"settlements": [...], "total": int}
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import inspect


def make_server():
    """Create a minimal CockpitServer for handler inspection."""
    from knarr.dashboard.server import CockpitServer

    mock_node = MagicMock()
    mock_node.storage = MagicMock()
    mock_node.storage.get_settlement_queue_page.return_value = ([], 0)

    server = CockpitServer.__new__(CockpitServer)
    server._node = mock_node
    server._routing_policy = {"defaults": {"local_weight": 1.0, "explicit_weight": 0.8, "remote_weight": 0.5}}
    return server


# ──────────────────────────────────────────────────────────────────────────────
# E-05-A: /api/settlements is routed in CockpitServer
# ──────────────────────────────────────────────────────────────────────────────

def test_settlements_endpoint_wired():
    """The /api/settlements path must be present in the request handler routing."""
    from knarr.dashboard import server as server_mod
    src = inspect.getsource(server_mod.CockpitServer)
    assert "/api/settlements" in src, "No /api/settlements route found in CockpitServer"


# ──────────────────────────────────────────────────────────────────────────────
# E-05-B: _handle_settlements_list method exists
# ──────────────────────────────────────────────────────────────────────────────

def test_handle_settlements_list_method_exists():
    """CockpitServer must have _handle_settlements_list method."""
    from knarr.dashboard.server import CockpitServer
    assert hasattr(CockpitServer, "_handle_settlements_list"), (
        "CockpitServer missing _handle_settlements_list method"
    )
    assert callable(CockpitServer._handle_settlements_list)


# ──────────────────────────────────────────────────────────────────────────────
# E-05-C: _handle_settlements_list uses storage.get_settlement_queue_page
# ──────────────────────────────────────────────────────────────────────────────

def test_settlements_list_uses_storage_method():
    """_handle_settlements_list must delegate to storage.get_settlement_queue_page."""
    from knarr.dashboard.server import CockpitServer
    src = inspect.getsource(CockpitServer._handle_settlements_list)
    assert "get_settlement_queue_page" in src, (
        "_handle_settlements_list does not call storage.get_settlement_queue_page"
    )
    # Must not call storage._get_conn() directly
    import re
    src_no_comments = re.sub(r'#[^\n]*', '', src)
    src_no_docs = re.sub(r'""".*?"""', '', src_no_comments, flags=re.DOTALL)
    assert "._get_conn()" not in src_no_docs, (
        "_handle_settlements_list still calls ._get_conn() directly"
    )


# ──────────────────────────────────────────────────────────────────────────────
# E-05-D: _handle_settlements_list calls storage with correct args
# ──────────────────────────────────────────────────────────────────────────────

import asyncio

def test_settlements_list_calls_storage():
    """_handle_settlements_list must call storage.get_settlement_queue_page."""
    from knarr.dashboard.server import CockpitServer

    mock_node = MagicMock()
    mock_storage = MagicMock()
    mock_storage.get_settlement_queue_page.return_value = (
        [{"id": 1, "settlement_type": "settle_request", "status": "pending"}], 1
    )
    mock_node.storage = mock_storage

    server = CockpitServer.__new__(CockpitServer)
    server._node = mock_node
    server._routing_policy = {"defaults": {"local_weight": 1.0}}

    # Capture _respond_json calls
    written_data = {}
    def fake_respond_json(writer, data):
        written_data.update(data)

    server._respond_json = fake_respond_json
    server._respond_error = MagicMock()

    mock_writer = MagicMock()
    query = {"limit": ["10"], "offset": ["0"]}

    asyncio.get_event_loop().run_until_complete(
        server._handle_settlements_list(mock_writer, query)
    )

    mock_storage.get_settlement_queue_page.assert_called_once_with(
        limit=10, offset=0, status_filter=None
    )
    assert "settlements" in written_data
    assert "total" in written_data
    assert written_data["total"] == 1


# ──────────────────────────────────────────────────────────────────────────────
# E-05-E: Invalid limit/offset returns 400
# ──────────────────────────────────────────────────────────────────────────────

def test_settlements_list_invalid_params():
    """_handle_settlements_list returns 400 for non-integer limit."""
    from knarr.dashboard.server import CockpitServer

    mock_node = MagicMock()
    mock_node.storage = MagicMock()

    server = CockpitServer.__new__(CockpitServer)
    server._node = mock_node
    server._routing_policy = {"defaults": {"local_weight": 1.0}}

    error_calls = []
    server._respond_json = MagicMock()
    server._respond_error = lambda w, code, msg: error_calls.append((code, msg))

    mock_writer = MagicMock()
    query = {"limit": ["not-a-number"], "offset": ["0"]}

    asyncio.get_event_loop().run_until_complete(
        server._handle_settlements_list(mock_writer, query)
    )

    assert len(error_calls) == 1
    assert error_calls[0][0] == 400
