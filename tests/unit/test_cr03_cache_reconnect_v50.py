"""CR-03: reconnect_from_cache() calls _self_populate_routing_table() on success.

BUG: reconnect_from_cache() returns True after successful cache-based reconnection
without calling _self_populate_routing_table(). Bootstrap addresses persist in the
routing table indefinitely after cache resume.

FIX: After successful cache reconnect (on success path, before return True), call
_self_populate_routing_table() via create_task.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


def _make_reconnect_node():
    """Create a minimal node stub for testing reconnect_from_cache."""
    from knarr.dht.node import DHTNode
    from knarr.core.models import NodeInfo

    node = MagicMock(spec=DHTNode)
    node._initial_bootstrap_peers = []
    node._bootstrap_peers = []
    node._ephemeral = False
    node._debug = False
    node.node_info = NodeInfo(node_id="aa" * 32, host="127.0.0.1", port=9000)

    storage = MagicMock()
    node.storage = storage

    node._enqueue_write_proto = AsyncMock()
    node._process_sync_response = AsyncMock()
    node._validate_peer_fields = MagicMock(return_value=True)
    node._self_populate_routing_table = AsyncMock()
    node.resolve_peer = MagicMock(return_value=("10.0.0.1", 9010))

    msg_mock = MagicMock()
    node._sign = lambda m: msg_mock

    return node


@pytest.mark.asyncio
async def test_self_populate_called_after_successful_cache_reconnect():
    """_self_populate_routing_table() must be called after successful cache reconnect."""
    from knarr.dht.node import DHTNode
    from knarr.core.messages import JoinResponse, SyncResponse
    from knarr.core.models import NodeInfo

    node = _make_reconnect_node()

    # Mock cached peers
    cached_peer = MagicMock()
    cached_peer.node_id = "bb" * 32
    cached_peer.host = "10.0.0.1"
    cached_peer.port = 9010
    node.storage.purge_stale_peers.return_value = 0
    node.storage.get_cached_peers.return_value = [cached_peer]

    # Mock successful JoinResponse
    join_resp = MagicMock(spec=JoinResponse)
    join_resp.peers = []

    populate_called = []

    async def fake_populate():
        populate_called.append(True)

    node._self_populate_routing_table = AsyncMock(side_effect=fake_populate)

    with patch("knarr.dht.node.request_response", return_value=join_resp):
        with patch("knarr.dht.node.verify_message", return_value=True):
            with patch("asyncio.get_running_loop") as mock_loop:
                loop = asyncio.get_event_loop()
                mock_loop.return_value = loop
                result = await DHTNode.reconnect_from_cache(node)

    assert result is True, f"reconnect_from_cache() returned {result}, expected True"

    # Give the create_task a chance to run
    await asyncio.sleep(0)

    assert len(populate_called) > 0, (
        "_self_populate_routing_table() was not called after successful cache reconnect. "
        "CR-03: must call _self_populate_routing_table() to evict bootstrap addresses."
    )


@pytest.mark.asyncio
async def test_reconnect_false_when_no_cached_peers():
    """reconnect_from_cache() returns False when no cached peers exist."""
    from knarr.dht.node import DHTNode

    node = _make_reconnect_node()
    node.storage.purge_stale_peers.return_value = 0
    node.storage.get_cached_peers.return_value = []

    result = await DHTNode.reconnect_from_cache(node)

    assert result is False
    node._self_populate_routing_table.assert_not_called()


@pytest.mark.asyncio
async def test_reconnect_false_when_all_peers_fail():
    """reconnect_from_cache() returns False when all cached peers time out."""
    from knarr.dht.node import DHTNode
    from knarr.core.models import NodeInfo

    node = _make_reconnect_node()

    cached_peer = MagicMock()
    cached_peer.node_id = "cc" * 32
    cached_peer.host = "10.0.0.1"
    cached_peer.port = 9010
    node.storage.purge_stale_peers.return_value = 0
    node.storage.get_cached_peers.return_value = [cached_peer]

    # Simulate failure: request_response raises exception
    with patch("knarr.dht.node.request_response", side_effect=Exception("connect timeout")):
        result = await DHTNode.reconnect_from_cache(node)

    assert result is False
    node._self_populate_routing_table.assert_not_called()
