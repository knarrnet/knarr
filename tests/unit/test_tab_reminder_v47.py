import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from knarr.commerce.handlers import make_commerce_handlers


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_node():
    node = MagicMock()
    node._enqueue_write = AsyncMock()
    node._sync = MagicMock()
    node._sync.enqueue = AsyncMock()
    node.storage = MagicMock()
    node._get_settlement_config = MagicMock(return_value={})
    return node


def test_tab_reminder_handler_is_registered():
    handlers = make_commerce_handlers(_make_node())
    assert "knarr/commerce/tab_reminder" in handlers


def test_valid_tab_reminder_logs_info_without_side_effects():
    node = _make_node()
    handlers = make_commerce_handlers(node)
    item = {
        "from_node": "abcdef1234567890abcdef1234567890",
        "body": {
            "type": "knarr/commerce/tab_reminder",
            "current_balance": -180.0,
            "credit_limit": 200.0,
            "utilization_pct": 90.0,
            "timestamp": time.time(),
        },
    }

    with patch("knarr.commerce.handlers.logger.info") as mock_info:
        _run(handlers["knarr/commerce/tab_reminder"](item))

    mock_info.assert_called_once()
    assert "abcdef1234567890" in mock_info.call_args.args[0]
    assert "balance=-180.0" in mock_info.call_args.args[0]
    assert "utilization_pct=90.0" in mock_info.call_args.args[0]
    node._enqueue_write.assert_not_called()
    node._sync.enqueue.assert_not_called()


def test_invalid_tab_reminder_logs_warning_without_crashing():
    node = _make_node()
    handlers = make_commerce_handlers(node)
    item = {
        "from_node": "abcdef1234567890abcdef1234567890",
        "body": {
            "type": "knarr/commerce/tab_reminder",
            "current_balance": -180.0,
            "timestamp": time.time(),
        },
    }

    with patch("knarr.commerce.handlers.logger.warning") as mock_warning:
        _run(handlers["knarr/commerce/tab_reminder"](item))

    mock_warning.assert_called_once()
    node._enqueue_write.assert_not_called()
    node._sync.enqueue.assert_not_called()


def test_tab_reminder_handler_has_no_settlement_or_netting_side_effects():
    node = _make_node()
    handlers = make_commerce_handlers(node)
    item = {
        "from_node": "abcdef1234567890abcdef1234567890",
        "body": {
            "type": "knarr/commerce/tab_reminder",
            "current_balance": -180.0,
            "credit_limit": 200.0,
            "utilization_pct": 90.0,
            "timestamp": time.time(),
        },
    }

    _run(handlers["knarr/commerce/tab_reminder"](item))

    node._enqueue_write.assert_not_called()
    node._sync.enqueue.assert_not_called()
