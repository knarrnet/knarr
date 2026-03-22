from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from knarr.core.messages import JoinResponse, SyncResponse
from knarr.core.models import NodeInfo
from knarr.dht.node import DHTNode


@pytest.mark.asyncio
async def test_join_releases_bootstrap_pool_connection_and_persists_peer():
    node = DHTNode.__new__(DHTNode)
    node._config = {"node": {"startup_jitter": False}}
    node._bootstrap_peers = []
    node._initial_bootstrap_peers = []
    node._debug = False
    node._ephemeral = False
    node.node_info = SimpleNamespace(node_id="aa" * 32, host="127.0.0.1", port=9999)
    node._sign = lambda msg: msg
    node._validate_peer_fields = lambda *_args: True
    node._reannounce_all = AsyncMock()
    node._self_populate_routing_table = AsyncMock()
    node._pool = SimpleNamespace(remove=AsyncMock())
    stored_peers = {}

    async def enqueue_write_proto(fn, peer):
        fn(peer)

    node._enqueue_write_proto = AsyncMock(side_effect=enqueue_write_proto)
    node.storage = SimpleNamespace(
        upsert_peer=lambda peer: stored_peers.__setitem__(peer.node_id, peer),
        get_peers=lambda: list(stored_peers.values()),
    )

    bootstrap_peer = {"node_id": "11" * 32, "host": "127.0.0.1", "port": 9000}
    extra_peer = {"node_id": "22" * 32, "host": "127.0.0.1", "port": 9001}

    async def fake_request_response(host, port, msg, timeout=5.0):
        if msg.type == "JOIN_REQUEST":
            return JoinResponse(peers=[bootstrap_peer, extra_peer])
        if msg.type == "SYNC_REQUEST":
            return SyncResponse(skills=[])
        raise AssertionError(f"unexpected message type: {msg.type}")

    with patch("knarr.dht.node.request_response", new=AsyncMock(side_effect=fake_request_response)), \
         patch("knarr.dht.node.verify_message", return_value=True):
        result = await node.join(["127.0.0.1:9000"], skip_jitter=True)

    assert result is True
    peers = {peer.node_id: (peer.host, peer.port) for peer in stored_peers.values()}
    assert peers["11" * 32] == ("127.0.0.1", 9000)
    node._pool.remove.assert_awaited_once_with("11" * 32)
