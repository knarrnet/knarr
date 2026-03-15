import sys
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from knarr.dht.node import DHTNode


def test_node_debug_flag_respects_config_true():
    node = DHTNode("127.0.0.1", 0, storage_path=":memory:", config={"node": {"debug": True}})
    assert node._debug is True


def test_node_debug_flag_defaults_false():
    node = DHTNode("127.0.0.1", 0, storage_path=":memory:")
    assert node._debug is False


@pytest.mark.asyncio
async def test_settlement_consumer_logs_exc_info_on_handler_failure():
    node = DHTNode.__new__(DHTNode)
    node.storage = MagicMock()
    node.storage.get_pending_settlements.return_value = [{"id": 7, "item_type": "soft_threshold"}]
    node.storage.mark_settlement_processed = MagicMock()
    node._process_settlement_item = AsyncMock(side_effect=RuntimeError("boom"))
    node._enqueue_write = AsyncMock()

    with patch("knarr.dht.node.logger.error") as mock_error:
        await DHTNode._settlement_consumer_tick(node)

    mock_error.assert_called_once()
    assert mock_error.call_args.kwargs.get("exc_info") is True
    node._enqueue_write.assert_awaited_once_with(node.storage.mark_settlement_processed, 7, "failed")


@pytest.mark.asyncio
async def test_debug_referencing_method_no_longer_raises_attribute_error():
    node = DHTNode("127.0.0.1", 0, storage_path=":memory:")
    node.storage.get_settlement_cadence = MagicMock(return_value=time.time())
    node._resolve_settlement_peer_key = MagicMock(return_value="ab" * 32)
    node._get_settlement_config = MagicMock(return_value={"min_interval_seconds": 60.0})

    await DHTNode._handle_settlement_soft_threshold(
        node,
        {"id": 1, "body": {"current_balance": -5.0}},
    )
