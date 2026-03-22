import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from knarr.dht.node import DHTNode


def _make_writer():
    writer = MagicMock()
    writer.get_extra_info.return_value = ("127.0.0.1", 9999)
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    return writer


def _make_reader():
    reader = MagicMock()
    reader.read = AsyncMock(return_value=b"")
    reader._buffer = bytearray()
    return reader


@pytest.mark.asyncio
async def test_connection_timeout_logs_no_error(caplog):
    node = DHTNode("127.0.0.1", 0, storage_path=":memory:")
    reader = _make_reader()
    writer = _make_writer()

    with patch("knarr.dht.node.receive_message", AsyncMock(side_effect=asyncio.TimeoutError())):
        with caplog.at_level("DEBUG", logger="knarr.dht.node"):
            await node._handle_connection(reader, writer)

    assert not [record for record in caplog.records if record.levelname == "ERROR"]
    assert any("CONNECTION_IDLE_TIMEOUT" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_connection_cancel_logs_no_error(caplog):
    node = DHTNode("127.0.0.1", 0, storage_path=":memory:")
    reader = _make_reader()
    writer = _make_writer()

    with patch("knarr.dht.node.receive_message", AsyncMock(side_effect=asyncio.CancelledError())):
        with caplog.at_level("DEBUG", logger="knarr.dht.node"):
            await node._handle_connection(reader, writer)

    assert not [record for record in caplog.records if record.levelname == "ERROR"]
    assert any("CONNECTION_CANCELLED" in record.message for record in caplog.records)
