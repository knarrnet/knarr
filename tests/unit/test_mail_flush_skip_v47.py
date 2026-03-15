import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from knarr.mail.sync import SyncEngine


def _make_node():
    node = MagicMock()
    node.node_info.node_id = "sender"
    node.storage = MagicMock()
    node.storage.get_outbox_recipients.return_value = []
    node.storage.get_peers.return_value = []
    node.storage.get_provider_address.return_value = None
    node.resolve_peer = MagicMock(return_value=("0.0.0.0", 0))
    node._config = {"mail": {}}
    node.bus = MagicMock()
    return node


@pytest.mark.asyncio
async def test_flush_outbox_emits_flush_skip_event_for_unresolvable_recipient():
    node = _make_node()
    node.storage.get_outbox_recipients.return_value = ["abcdef1234567890abcdef1234567890"]
    engine = SyncEngine(node)
    engine.push_to_peer = AsyncMock()

    await engine.flush_outbox()

    node.bus.emit.assert_called_once_with("mail.flush_skip", to_node="abcdef1234567890")
    engine.push_to_peer.assert_not_called()


@pytest.mark.asyncio
async def test_flush_outbox_does_not_emit_when_recipient_resolves():
    node = _make_node()
    node.storage.get_outbox_recipients.return_value = ["abcdef1234567890abcdef1234567890"]
    node.resolve_peer.return_value = ("127.0.0.1", 9010)
    engine = SyncEngine(node)
    engine.push_to_peer = AsyncMock()

    await engine.flush_outbox()

    node.bus.emit.assert_not_called()
    engine.push_to_peer.assert_awaited_once_with("abcdef1234567890abcdef1234567890", "127.0.0.1", 9010)


@pytest.mark.asyncio
async def test_flush_outbox_without_bus_still_logs_warning():
    node = _make_node()
    node.storage.get_outbox_recipients.return_value = ["abcdef1234567890abcdef1234567890"]
    node.bus = None
    engine = SyncEngine(node)
    engine.push_to_peer = AsyncMock()
    engine._log = MagicMock()

    await engine.flush_outbox()

    engine._log.warning.assert_called_once()
    engine.push_to_peer.assert_not_called()
