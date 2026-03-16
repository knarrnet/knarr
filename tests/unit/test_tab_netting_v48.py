"""Tests for BR-MAIL-001-EXT: tab_reminder auto-netting."""
import asyncio
import json
import time
import unittest
from unittest.mock import MagicMock, patch


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        except Exception:
            pass
        asyncio.set_event_loop(asyncio.new_event_loop())


def _make_tab_reminder_item(utilization_pct=85.0, balance=-4.0, limit=5.0):
    """Create a valid tab_reminder mail item."""
    return {
        "from_node": "a" * 64,
        "body": json.dumps({
            "type": "knarr/commerce/tab_reminder",
            "current_balance": balance,
            "credit_limit": limit,
            "utilization_pct": utilization_pct,
            "timestamp": time.time(),
            "schema_version": "1.0",
        }),
    }


class TestTabReminderAutoNetting(unittest.TestCase):
    """handle_tab_reminder must auto-trigger netting when configured and due."""

    def _get_handler(self, node):
        from knarr.commerce.handlers import make_commerce_handlers

        handlers = make_commerce_handlers(node)
        return handlers["knarr/commerce/tab_reminder"]

    def test_auto_netting_triggered_when_opted_in_and_over_threshold(self):
        node = MagicMock()
        node._get_settlement_config.return_value = {
            "tab_reminder_auto_netting": True,
            "tab_reminder_threshold": 80.0,
        }

        handler = self._get_handler(node)
        _run_async(handler(_make_tab_reminder_item(utilization_pct=85.0)))

        node._run_netting_cycle_if_due.assert_called_once()

    def test_auto_netting_not_triggered_when_opted_out(self):
        node = MagicMock()
        node._get_settlement_config.return_value = {
            "tab_reminder_auto_netting": False,
            "tab_reminder_threshold": 80.0,
        }

        handler = self._get_handler(node)
        _run_async(handler(_make_tab_reminder_item(utilization_pct=85.0)))

        node._run_netting_cycle_if_due.assert_not_called()

    def test_auto_netting_not_triggered_below_threshold(self):
        node = MagicMock()
        node._get_settlement_config.return_value = {
            "tab_reminder_auto_netting": True,
            "tab_reminder_threshold": 80.0,
        }

        handler = self._get_handler(node)
        _run_async(handler(_make_tab_reminder_item(utilization_pct=50.0)))

        node._run_netting_cycle_if_due.assert_not_called()

    def test_auto_netting_triggered_at_exact_threshold(self):
        node = MagicMock()
        node._get_settlement_config.return_value = {
            "tab_reminder_auto_netting": True,
            "tab_reminder_threshold": 80.0,
        }

        handler = self._get_handler(node)
        _run_async(handler(_make_tab_reminder_item(utilization_pct=80.0)))

        node._run_netting_cycle_if_due.assert_called_once()

    def test_auto_netting_default_opt_out(self):
        node = MagicMock()
        node._get_settlement_config.return_value = {}

        handler = self._get_handler(node)
        _run_async(handler(_make_tab_reminder_item(utilization_pct=90.0)))

        node._run_netting_cycle_if_due.assert_not_called()

    def test_existing_info_log_behavior_is_preserved(self):
        node = MagicMock()
        node._get_settlement_config.return_value = {
            "tab_reminder_auto_netting": True,
            "tab_reminder_threshold": 80.0,
        }

        handler = self._get_handler(node)
        with patch("knarr.commerce.handlers.logger.info") as mock_info:
            _run_async(handler(_make_tab_reminder_item(utilization_pct=90.0)))

        mock_info.assert_called_once()
        node._run_netting_cycle_if_due.assert_called_once()


if __name__ == "__main__":
    unittest.main()
