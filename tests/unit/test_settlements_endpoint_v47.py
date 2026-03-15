"""Tests for B1 — GET /api/settlements endpoint."""
import asyncio
import json
import time
import pytest
from unittest.mock import MagicMock, AsyncMock
from knarr.dht.storage import Storage


def make_server_with_storage(storage=None):
    """Create a CockpitServer with a real or mock storage."""
    from knarr.dashboard.server import CockpitServer

    node = MagicMock()
    node.storage = storage or Storage(":memory:")

    server = CockpitServer.__new__(CockpitServer)
    server._node = node
    server._auth_token = ""
    return server


def populate_settlements(storage, items):
    """Insert test items into settlement_queue."""
    conn = storage._get_conn()
    for item in items:
        conn.execute(
            "INSERT INTO settlement_queue (item_type, from_node, body, priority, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                item.get("item_type", "soft_threshold"),
                item.get("from_node", "aa" * 32),
                json.dumps(item.get("body", {})),
                item.get("priority", 0),
                item.get("status", "pending"),
                item.get("created_at", time.time()),
            ),
        )
    conn.commit()


def capture_response(server, query=None):
    """Call _handle_settlements_list and capture the JSON response."""
    responses = []

    class FakeWriter:
        def write(self, data):
            responses.append(data)
        def get_extra_info(self, key, default=None):
            return default

    writer = FakeWriter()

    # Patch _respond_json to capture output
    def mock_respond_json(w, data):
        responses.append(data)

    server._respond_json = mock_respond_json
    server._respond_error = lambda w, code, msg: responses.append({"error": msg, "code": code})

    asyncio.get_event_loop().run_until_complete(
        server._handle_settlements_list(writer, query or {})
    )
    return responses[0] if responses else None


class TestSettlementsEndpoint:
    def test_get_returns_200_with_settlements_list_and_total(self):
        """GET /api/settlements returns dict with 'settlements' list and 'total'."""
        storage = Storage(":memory:")
        populate_settlements(storage, [
            {"item_type": "soft_threshold", "body": {"amount": 10.0}},
            {"item_type": "settle_request", "body": {"amount": 5.0}},
        ])
        server = make_server_with_storage(storage)
        result = capture_response(server)

        assert "settlements" in result, f"Missing 'settlements' key: {result}"
        assert "total" in result, f"Missing 'total' key: {result}"
        assert result["total"] == 2
        assert len(result["settlements"]) == 2

    def test_status_filter_returns_only_matching(self):
        """?status=pending filters correctly."""
        storage = Storage(":memory:")
        populate_settlements(storage, [
            {"status": "pending", "body": {}},
            {"status": "processed", "body": {}},
            {"status": "pending", "body": {}},
        ])
        server = make_server_with_storage(storage)
        result = capture_response(server, {"status": ["pending"]})

        assert result["total"] == 2
        for s in result["settlements"]:
            assert s["status"] == "pending", f"Non-pending item returned: {s}"

    def test_pagination_limit_and_offset(self):
        """?limit=2&offset=1 respects pagination."""
        storage = Storage(":memory:")
        populate_settlements(storage, [
            {"body": {}},
            {"body": {}},
            {"body": {}},
            {"body": {}},
        ])
        server = make_server_with_storage(storage)
        result = capture_response(server, {"limit": ["2"], "offset": ["1"]})

        assert len(result["settlements"]) == 2
        assert result["total"] == 4  # total is count of all, not page

    def test_empty_queue_returns_empty_list(self):
        """Empty queue -> {'settlements': [], 'total': 0}."""
        storage = Storage(":memory:")
        server = make_server_with_storage(storage)
        result = capture_response(server)

        assert result["settlements"] == []
        assert result["total"] == 0

    def test_body_parsed_as_object(self):
        """'body' stored as JSON text -> returned as parsed object."""
        storage = Storage(":memory:")
        populate_settlements(storage, [
            {"body": {"peer_key": "abc", "amount": 42.0}},
        ])
        server = make_server_with_storage(storage)
        result = capture_response(server)

        body = result["settlements"][0]["body"]
        assert isinstance(body, dict), f"body should be dict, got {type(body)}: {body}"
        assert body.get("peer_key") == "abc"

    def test_column_names_match_settlement_queue(self):
        """Response uses exact settlement_queue column names — no aliasing."""
        storage = Storage(":memory:")
        populate_settlements(storage, [{"body": {}}])
        server = make_server_with_storage(storage)
        result = capture_response(server)

        s = result["settlements"][0]
        expected_keys = {"id", "item_type", "from_node", "body", "priority", "status",
                         "created_at", "processed_at"}
        assert expected_keys.issubset(s.keys()), (
            f"Missing column keys. Expected {expected_keys}, got {set(s.keys())}"
        )
        # Must NOT have aliased names
        assert "counterparty_node_id" not in s, "Must not alias to counterparty_node_id"
        assert "amount" not in s, "Must not alias to amount"
        assert "confirmed_at" not in s, "Must not alias to confirmed_at"

    def test_auth_check_rejects_missing_bearer_token(self):
        """Endpoint is protected — _check_auth returns False with no auth header."""
        server = make_server_with_storage()
        server._auth_token = "secrettoken"
        # No Authorization header
        assert server._check_auth({}) is False

    def test_auth_check_rejects_wrong_token(self):
        """`_check_auth` returns False with wrong token."""
        server = make_server_with_storage()
        server._auth_token = "secrettoken"
        assert server._check_auth({"authorization": "Bearer wrongtoken"}) is False

    def test_auth_check_accepts_correct_token(self):
        """`_check_auth` returns True with correct Bearer token."""
        server = make_server_with_storage()
        server._auth_token = "secrettoken"
        assert server._check_auth({"authorization": "Bearer secrettoken"}) is True
