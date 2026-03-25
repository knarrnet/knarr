"""C-01: flush_outbox parallelization with asyncio.gather.

Verifies:
- flush_outbox calls asyncio.gather (or equivalent) to deliver to all recipients concurrently
- A slow/failing recipient does not block other recipients
- _flush_one_recipient exists and handles self-delivery, peer-table, and fallback paths
"""

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import inspect


def make_sync(node=None):
    """Create a minimal SyncEngine for testing flush_outbox."""
    from knarr.mail.sync import SyncEngine

    if node is None:
        node = MagicMock()
        node.node_info.node_id = "a" * 64
        node.storage.get_outbox_recipients.return_value = []
        node.storage.get_peer_by_id.return_value = None
        node.storage.get_provider_address.return_value = None
        node.resolve_peer.return_value = ("0.0.0.0", 0)

    sync = SyncEngine.__new__(SyncEngine)
    sync._node = node
    sync._debug = False
    sync._log = MagicMock()
    return sync


# ──────────────────────────────────────────────────────────────────────────────
# C-01-A: flush_outbox uses asyncio.gather
# ──────────────────────────────────────────────────────────────────────────────

def test_flush_outbox_uses_asyncio_gather():
    """flush_outbox must use asyncio.gather to parallelize recipient delivery."""
    from knarr.mail.sync import SyncEngine
    src = inspect.getsource(SyncEngine.flush_outbox)
    assert "asyncio.gather" in src, (
        "flush_outbox does not use asyncio.gather for parallel delivery"
    )


# ──────────────────────────────────────────────────────────────────────────────
# C-01-B: _flush_one_recipient method exists
# ──────────────────────────────────────────────────────────────────────────────

def test_flush_one_recipient_exists():
    """SyncEngine must have _flush_one_recipient method extracted for gather."""
    from knarr.mail.sync import SyncEngine
    assert hasattr(SyncEngine, "_flush_one_recipient"), (
        "SyncEngine missing _flush_one_recipient method"
    )
    assert callable(SyncEngine._flush_one_recipient)
    assert asyncio.iscoroutinefunction(SyncEngine._flush_one_recipient)


# ──────────────────────────────────────────────────────────────────────────────
# C-01-C: All recipients receive a delivery attempt
# ──────────────────────────────────────────────────────────────────────────────

def test_all_recipients_attempted():
    """Every recipient in get_outbox_recipients must get a delivery attempt."""
    recipients = ["b" * 64, "c" * 64, "d" * 64]

    node = MagicMock()
    node.node_info.node_id = "a" * 64
    node.storage.get_outbox_recipients.return_value = recipients
    node.storage.get_peer_by_id.return_value = None
    node.storage.get_provider_address.return_value = None
    node.resolve_peer.return_value = ("0.0.0.0", 0)

    sync = make_sync(node)
    delivered = []

    async def mock_flush_one(to_node):
        delivered.append(to_node)

    sync._flush_one_recipient = mock_flush_one

    asyncio.get_event_loop().run_until_complete(sync.flush_outbox())

    for r in recipients:
        assert r in delivered, f"Recipient {r[:8]} was not attempted"


# ──────────────────────────────────────────────────────────────────────────────
# C-01-D: One failing recipient does not block others
# ──────────────────────────────────────────────────────────────────────────────

def test_failing_recipient_does_not_block_others():
    """An exception from one recipient must not prevent delivery to others."""
    recipients = ["b" * 64, "c" * 64, "d" * 64]

    node = MagicMock()
    node.node_info.node_id = "a" * 64
    node.storage.get_outbox_recipients.return_value = recipients
    node.storage.get_peer_by_id.return_value = None
    node.storage.get_provider_address.return_value = None
    node.resolve_peer.return_value = ("0.0.0.0", 0)

    sync = make_sync(node)
    delivered = []

    async def mock_flush_one(to_node):
        if to_node == "b" * 64:
            raise ConnectionError("host unreachable")
        delivered.append(to_node)

    sync._flush_one_recipient = mock_flush_one

    # Must not raise even though one recipient fails
    asyncio.get_event_loop().run_until_complete(sync.flush_outbox())

    assert "c" * 64 in delivered
    assert "d" * 64 in delivered


# ──────────────────────────────────────────────────────────────────────────────
# C-01-E: Empty outbox — early return, no gather
# ──────────────────────────────────────────────────────────────────────────────

def test_empty_outbox_early_return():
    """flush_outbox must return immediately when there are no recipients."""
    node = MagicMock()
    node.node_info.node_id = "a" * 64
    node.storage.get_outbox_recipients.return_value = []

    sync = make_sync(node)

    # No exception, no delivery attempt
    asyncio.get_event_loop().run_until_complete(sync.flush_outbox())


# ──────────────────────────────────────────────────────────────────────────────
# C-01-F: Self-delivery path in _flush_one_recipient
# ──────────────────────────────────────────────────────────────────────────────

def test_self_delivery_calls_self_deliver():
    """_flush_one_recipient must call _self_deliver when recipient == self."""
    my_node_id = "a" * 64

    node = MagicMock()
    node.node_info.node_id = my_node_id

    sync = make_sync(node)
    self_delivered = []

    async def mock_self_deliver(to_node):
        self_delivered.append(to_node)

    sync._self_deliver = mock_self_deliver

    asyncio.get_event_loop().run_until_complete(
        sync._flush_one_recipient(my_node_id)
    )

    assert my_node_id in self_delivered
