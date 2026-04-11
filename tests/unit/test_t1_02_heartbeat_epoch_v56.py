import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from knarr.core.messages import Heartbeat
from knarr.dht.node import DHTNode


def test_get_punchhole_epoch_reads_backend_plugin(caplog):
    node = DHTNode.__new__(DHTNode)
    backend = SimpleNamespace(get_punchhole_epoch=lambda: 7)
    node._plugins = SimpleNamespace(get_plugin_by_name=lambda name: backend if name == "punchhole-backend" else None)

    with caplog.at_level("DEBUG", logger="knarr.dht.node"):
        assert node._get_punchhole_epoch() == 7

    assert any(record.getMessage() == "HB_EPOCH_READ epoch=7" for record in caplog.records)


def test_all_heartbeat_sites_use_epoch_helper():
    assert "punchhole_epoch=self._get_punchhole_epoch()" in inspect.getsource(DHTNode._process_message)
    assert "punchhole_epoch=self._get_punchhole_epoch()" in inspect.getsource(DHTNode.force_heartbeat)
    assert "punchhole_epoch=self._get_punchhole_epoch()" in inspect.getsource(DHTNode._push_to_peer_cb)
    assert "punchhole_epoch=self._get_punchhole_epoch()" in inspect.getsource(DHTNode._peer_heartbeat_sweep)


@pytest.mark.asyncio
async def test_force_heartbeat_sends_backend_epoch():
    node = DHTNode.__new__(DHTNode)
    node.node_info = SimpleNamespace(node_id="a" * 64)
    node._config = {"mail": {"debug": False}}
    node._peer_last_activity = {}
    node._sync = SimpleNamespace(push_to_peer=AsyncMock())
    node.storage = SimpleNamespace(
        get_peer_by_id=lambda node_id: SimpleNamespace(node_id=node_id, host="127.0.0.1", port=9010)
    )
    node.resolve_peer = lambda node_id, host, port: (host, port)
    node._sign = lambda msg: msg
    node._get_punchhole_epoch = lambda: 11

    sent = []

    async def _send(peer_id, host, port, msg):
        sent.append(msg)
        return Heartbeat(node_id=peer_id, version="0.56.0")

    node._pool = SimpleNamespace(send=AsyncMock(side_effect=_send))

    with patch("knarr.dht.node.verify_message", return_value=True), patch(
        "knarr.dht.node.verify_node_id", return_value=True
    ):
        result = await node.force_heartbeat("b" * 64)

    assert result["status"] == "ok"
    assert sent[0].punchhole_epoch == 11
