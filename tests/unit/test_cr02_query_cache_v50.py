from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from knarr.core.messages import QueryResponse
from knarr.dht.node import DHTNode


def _remote_result(node_id: str, name: str = "translate") -> dict:
    return {
        "node_id": node_id,
        "host": "127.0.0.1",
        "port": 9401,
        "sidecar_port": 9501,
        "skill_sheet": {
            "name": name,
            "version": "1.0.0",
            "description": "Translate text",
            "tags": ["text"],
            "input_schema": {},
            "output_schema": {},
        },
    }


def _make_node() -> DHTNode:
    node = DHTNode("127.0.0.1", 0, storage_path=":memory:")
    node._plugins.on_query = AsyncMock(return_value=[])
    node._sign = lambda msg: msg
    node._rank_results = lambda results: results
    node._filter_providers_by_group = lambda results: results
    return node


@pytest.mark.asyncio
async def test_network_query_results_are_cached():
    node = _make_node()
    node.storage.get_peers = MagicMock(return_value=[MagicMock(host="127.0.0.1", port=9010)])
    node._enqueue_write = AsyncMock()
    remote = _remote_result("b" * 64)

    with patch("knarr.dht.node.request_response", AsyncMock(return_value=QueryResponse(results=[remote]))):
        with patch("knarr.dht.node.verify_message", return_value=True):
            await node.query("name", "translate")

    cached = [call for call in node._enqueue_write.call_args_list if call.args[0] == node.storage.upsert_skill]
    assert len(cached) == 1
    assert cached[0].args[1] == "translate"
    assert cached[0].args[2] == remote["node_id"]


@pytest.mark.asyncio
async def test_self_node_results_are_not_cached():
    node = _make_node()
    node.storage.get_peers = MagicMock(return_value=[MagicMock(host="127.0.0.1", port=9010)])
    node._enqueue_write = AsyncMock()
    remote = _remote_result(node.node_info.node_id)

    with patch("knarr.dht.node.request_response", AsyncMock(return_value=QueryResponse(results=[remote]))):
        with patch("knarr.dht.node.verify_message", return_value=True):
            await node.query("name", "translate")

    cached = [call for call in node._enqueue_write.call_args_list if call.args[0] == node.storage.upsert_skill]
    assert cached == []


@pytest.mark.asyncio
async def test_second_query_uses_local_cache_without_network_call():
    node = _make_node()
    node.storage.get_peers = MagicMock(return_value=[MagicMock(host="127.0.0.1", port=9010)])
    remote = _remote_result("c" * 64)

    async def direct_write(func, *args):
        return func(*args)

    node._enqueue_write = direct_write

    with patch("knarr.dht.node.request_response", AsyncMock(return_value=QueryResponse(results=[remote]))):
        with patch("knarr.dht.node.verify_message", return_value=True):
            first = await node.query("name", "translate")

    with patch("knarr.dht.node.request_response", AsyncMock(side_effect=AssertionError("network call should be skipped"))):
        second = await node.query("name", "translate")

    assert len(first) == 1
    assert len(second) == 1
    assert second[0]["node_id"] == remote["node_id"]
    assert second[0]["skill_sheet"]["name"] == "translate"
