"""CR-05: GET /api/demand cockpit endpoint returns demand summary.

BUG: get_demand_summary() exists on the node and records zero-result queries.
No cockpit endpoint exposed this data.

FIX: Add GET /api/demand route to cockpit server.py that calls
node.get_demand_summary() and returns JSON: {"demands": [...], "total": N}.
"""
import asyncio
import json
import pytest
from unittest.mock import MagicMock
from knarr.dashboard.server import CockpitServer


class MockNodeWithDemand:
    """Minimal node stub for demand endpoint tests."""

    def get_status(self):
        return {"status": "ok"}

    def get_peers(self):
        return []

    def get_skills(self):
        return {"local": [], "network": []}

    def get_tasks(self):
        return []

    def get_ledger(self):
        return []

    def get_demand_summary(self):
        return [
            {"skill": "test-skill", "count": 5, "last_seen": 1710000000.0},
            {"skill": "other-skill", "count": 2, "last_seen": 1710000100.0},
        ]


@pytest.mark.asyncio
async def test_demand_endpoint_returns_json():
    """GET /api/demand must return 200 JSON with demands list and total count."""
    node = MockNodeWithDemand()
    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        req = "GET /api/demand HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        writer.write(req.encode())
        await writer.drain()

        response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
        writer.close()
        await writer.wait_closed()

        assert b"200 OK" in response, (
            f"Expected 200 OK from /api/demand, got: {response[:200]}"
        )
        assert b"application/json" in response, (
            "Expected Content-Type: application/json in response"
        )

        body_bytes = response.split(b"\r\n\r\n", 1)[1]
        data = json.loads(body_bytes)

        assert "demands" in data, (
            f"CR-05: /api/demand response missing 'demands' key. Got: {list(data.keys())}"
        )
        assert "total" in data, (
            f"CR-05: /api/demand response missing 'total' key. Got: {list(data.keys())}"
        )
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_demand_endpoint_total_matches_list_length():
    """GET /api/demand total must equal len(demands)."""
    node = MockNodeWithDemand()
    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        req = "GET /api/demand HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        writer.write(req.encode())
        await writer.drain()

        response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
        writer.close()
        await writer.wait_closed()

        body_bytes = response.split(b"\r\n\r\n", 1)[1]
        data = json.loads(body_bytes)

        assert data["total"] == len(data["demands"]), (
            f"CR-05: total={data['total']} does not match len(demands)={len(data['demands'])}"
        )
        assert data["total"] == 2, (
            f"CR-05: expected total=2, got total={data['total']}"
        )
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_demand_endpoint_empty_list():
    """GET /api/demand with no demand records returns empty list with total=0."""
    class EmptyDemandNode(MockNodeWithDemand):
        def get_demand_summary(self):
            return []

    node = EmptyDemandNode()
    server = CockpitServer(node, bind="127.0.0.1", port=0)
    await server.start()
    port = server.port

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        req = "GET /api/demand HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        writer.write(req.encode())
        await writer.drain()

        response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
        writer.close()
        await writer.wait_closed()

        body_bytes = response.split(b"\r\n\r\n", 1)[1]
        data = json.loads(body_bytes)

        assert data["demands"] == [], f"Expected empty demands list, got {data['demands']}"
        assert data["total"] == 0, f"Expected total=0, got {data['total']}"
    finally:
        await server.stop()
