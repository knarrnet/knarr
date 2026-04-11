from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from knarr.dht.node import DHTNode
from knarr.mail.sync import SyncEngine


@pytest.mark.asyncio
async def test_reannounce_all_fetches_peers_via_protocol_pool():
    node = DHTNode.__new__(DHTNode)
    node._version_gated = False
    node.storage = SimpleNamespace(get_peers=MagicMock())
    node._run_in_protocol_pool = AsyncMock(return_value=[])

    await node._reannounce_all()

    node._run_in_protocol_pool.assert_awaited_once_with(node.storage.get_peers)


@pytest.mark.asyncio
async def test_evict_bootstrap_peers_fetches_peers_via_protocol_pool():
    node = DHTNode.__new__(DHTNode)
    node._initial_bootstrap_peers = ["bootstrap.example:9000"]
    node.storage = SimpleNamespace(get_peers=MagicMock())
    node._run_in_protocol_pool = AsyncMock(return_value=[])

    await node._evict_bootstrap_peers()

    node._run_in_protocol_pool.assert_awaited_once_with(node.storage.get_peers)


@pytest.mark.asyncio
async def test_flush_outbox_fetches_recipients_via_protocol_pool():
    storage = SimpleNamespace(get_outbox_recipients=MagicMock())
    node = SimpleNamespace(
        _config={"mail": {}},
        storage=storage,
        _run_in_protocol_pool=AsyncMock(return_value=[]),
        node_info=SimpleNamespace(node_id="n" * 64),
    )
    sync = SyncEngine(node)

    await sync.flush_outbox()

    node._run_in_protocol_pool.assert_awaited_once_with(storage.get_outbox_recipients)


@pytest.mark.asyncio
async def test_flush_one_recipient_uses_protocol_pool_for_route_lookups_and_writer_for_abandon():
    to_node = "a" * 64
    storage = SimpleNamespace(
        get_peer_by_id=MagicMock(return_value=None),
        get_provider_address=MagicMock(return_value=None),
        abandon_outbox=MagicMock(),
    )

    async def _run_in_protocol_pool(fn, *args):
        return fn(*args)

    node = SimpleNamespace(
        _config={"mail": {}},
        storage=storage,
        _run_in_protocol_pool=AsyncMock(side_effect=_run_in_protocol_pool),
        _enqueue_write=AsyncMock(return_value=3),
        resolve_peer=MagicMock(return_value=("0.0.0.0", 0)),
        node_info=SimpleNamespace(node_id="b" * 64),
        bus=None,
    )
    sync = SyncEngine(node)
    sync._flush_skip_count[to_node] = sync._flush_skip_max - 1
    sync.push_to_peer = AsyncMock()

    await sync._flush_one_recipient(to_node)

    assert node._run_in_protocol_pool.await_args_list[0].args == (storage.get_peer_by_id, to_node)
    assert node._run_in_protocol_pool.await_args_list[1].args == (storage.get_provider_address, to_node)
    node._enqueue_write.assert_awaited_once_with(storage.abandon_outbox, to_node)


@pytest.mark.asyncio
async def test_self_deliver_reads_pending_outbox_via_protocol_pool():
    storage = SimpleNamespace(get_pending_outbox=MagicMock(return_value=[]))

    async def _run_in_protocol_pool(fn, *args):
        return fn(*args)

    node = SimpleNamespace(
        _config={"mail": {}},
        storage=storage,
        _run_in_protocol_pool=AsyncMock(side_effect=_run_in_protocol_pool),
        node_info=SimpleNamespace(node_id="c" * 64),
    )
    sync = SyncEngine(node)

    await sync._self_deliver(node.node_info.node_id)

    node._run_in_protocol_pool.assert_awaited_once_with(storage.get_pending_outbox, node.node_info.node_id, 50)
