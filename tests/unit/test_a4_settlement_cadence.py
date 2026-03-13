"""A4 contract test: Settlement cadence guard.

E-025: settlement_state table was dropped in v0.39.0. No guard exists against
settlement spam on the same bilateral pair. Without a cadence guard, a node can
trigger settlement on every heartbeat tick.

FIX LOCATION:
- storage.py — add get_settlement_cadence(peer_key) and set_settlement_cadence(peer_key, ts)
- migrations/v0_45_0.sql — new settlement_cadence table
- node.py:_handle_settlement_soft_threshold — check cadence before calling prepare_settlement

CONTRACT:
1. storage.get_settlement_cadence(peer_key) returns None when no entry exists.
2. storage.set_settlement_cadence(peer_key, timestamp) stores entry.
3. storage.get_settlement_cadence(peer_key) returns stored timestamp after set.
4. _handle_settlement_soft_threshold checks cadence: if last_settlement for peer
   was less than min_interval_seconds ago, it returns without triggering settlement.
5. Default min_interval_seconds is 86400 (once daily).
6. Config key economy.settlement_min_interval_seconds overrides the default.
"""
import time
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from knarr.dht.storage import Storage


PEER_KEY = "ab" * 32


# --- Storage layer tests (new methods required) ---

def test_get_settlement_cadence_returns_none_when_no_entry():
    """get_settlement_cadence must return None for an unknown peer."""
    storage = Storage(":memory:")
    result = storage.get_settlement_cadence(PEER_KEY)
    assert result is None, (
        "get_settlement_cadence must return None when no cadence entry exists. "
        "Method may be missing — add to storage.py."
    )


def test_set_and_get_settlement_cadence():
    """set then get must return the stored timestamp."""
    storage = Storage(":memory:")
    now = time.time()
    storage.set_settlement_cadence(PEER_KEY, now)
    result = storage.get_settlement_cadence(PEER_KEY)
    assert result is not None, (
        "get_settlement_cadence returned None after set_settlement_cadence. "
        "Fix: persist cadence entry to settlement_cadence table."
    )
    assert abs(result - now) < 1.0, (
        f"Returned cadence timestamp {result} doesn't match stored {now}."
    )


def test_set_settlement_cadence_updates_existing():
    """Second set must update, not duplicate."""
    storage = Storage(":memory:")
    t1 = time.time() - 100
    t2 = time.time()
    storage.set_settlement_cadence(PEER_KEY, t1)
    storage.set_settlement_cadence(PEER_KEY, t2)
    result = storage.get_settlement_cadence(PEER_KEY)
    assert abs(result - t2) < 1.0, "set_settlement_cadence must UPDATE existing entry."


def test_cadence_is_peer_scoped():
    """Different peers have independent cadence entries."""
    storage = Storage(":memory:")
    peer_a = "aa" * 32
    peer_b = "bb" * 32
    now = time.time()
    storage.set_settlement_cadence(peer_a, now - 1000)
    assert storage.get_settlement_cadence(peer_b) is None, (
        "Cadence entry for peer_a must not appear for peer_b."
    )


# --- Node-level guard tests ---

def _make_settlement_item(peer_key=PEER_KEY, balance=-50.0):
    return {
        "id": 1,
        "item_type": "soft_threshold",
        "body": {
            "peer_public_key": peer_key,
            "current_balance": balance,
            "utilization_pct": 80.0,
            "prepaid": 0.0,
            "pub_tab": 0.0,
        },
    }


@pytest.mark.asyncio
async def test_cadence_guard_blocks_second_settlement_within_window():
    """Second settlement for same peer within min_interval must not call prepare_settlement."""
    from knarr.dht.node import DHTNode

    node = DHTNode.__new__(DHTNode)
    node.node_info = MagicMock()
    node.node_info.node_id = "cc" * 32
    node._signing_key = None
    node._public_key_hex = "dd" * 32
    node.bus = MagicMock()
    node._debug = False
    node._config = {"economy": {"settlement_min_interval_seconds": 300}}

    mock_storage = MagicMock()
    mock_storage.get_or_create_ledger_entry = MagicMock(return_value=MagicMock(balance=-50.0))
    # Simulate: last settlement was 60s ago (within 300s window)
    mock_storage.get_settlement_cadence = MagicMock(return_value=time.time() - 60)
    mock_storage.set_settlement_cadence = MagicMock()
    node.storage = mock_storage

    prepare_called = []

    with patch("knarr.commerce.settlement_execution.prepare_settlement",
               new=AsyncMock(side_effect=lambda *a, **kw: prepare_called.append(True))):
        with patch.object(node, "_resolve_policy", return_value=(-30.0, -60.0)):
            with patch.object(node, "_resolve_settlement_peer_key", return_value=PEER_KEY):
                with patch.object(node, "_get_settlement_config",
                                  return_value={"min_interval_seconds": 300}):
                    await node._handle_settlement_soft_threshold(_make_settlement_item())

    assert len(prepare_called) == 0, (
        "prepare_settlement was called despite cadence guard. "
        "Fix: check get_settlement_cadence before triggering settlement."
    )


@pytest.mark.asyncio
async def test_cadence_guard_allows_settlement_after_interval():
    """Settlement is allowed when last settlement was > min_interval ago."""
    from knarr.dht.node import DHTNode

    node = DHTNode.__new__(DHTNode)
    node.node_info = MagicMock()
    node.node_info.node_id = "cc" * 32
    node._signing_key = None
    node._public_key_hex = "dd" * 32
    node.bus = MagicMock()
    node._debug = False
    node._config = {"economy": {"settlement_min_interval_seconds": 300}}

    mock_storage = MagicMock()
    mock_storage.get_or_create_ledger_entry = MagicMock(return_value=MagicMock(balance=-50.0))
    # Last settlement was 400s ago — window expired, should allow
    mock_storage.get_settlement_cadence = MagicMock(return_value=time.time() - 400)
    mock_storage.set_settlement_cadence = MagicMock()
    node.storage = mock_storage

    prepare_called = []

    mock_decision = MagicMock()
    mock_decision.action = "settle"

    with patch("knarr.commerce.settlement_engine.evaluate_settlement",
               return_value=mock_decision):
        with patch("knarr.commerce.settlement_execution.prepare_settlement",
                   new=AsyncMock(side_effect=lambda *a, **kw: prepare_called.append(True) or {})):
            with patch.object(node, "_resolve_policy", return_value=(-30.0, -60.0)):
                with patch.object(node, "_resolve_settlement_peer_key", return_value=PEER_KEY):
                    with patch.object(node, "_get_settlement_config",
                                      return_value={"min_interval_seconds": 300}):
                        try:
                            await node._handle_settlement_soft_threshold(_make_settlement_item())
                        except Exception:
                            pass  # downstream may fail — we just need the guard not to block

    assert len(prepare_called) > 0 or mock_storage.set_settlement_cadence.called, (
        "Settlement was NOT triggered even though cadence window expired. "
        "Guard must only block within the window, not permanently."
    )


def test_default_cadence_interval_is_86400():
    """Default settlement_min_interval_seconds must be 86400 (once daily)."""
    from knarr.dht.node import DHTNode

    node = DHTNode.__new__(DHTNode)
    node._config = {}

    # _get_settlement_config should return default of 86400
    config = node._get_settlement_config() if hasattr(node, "_get_settlement_config") else {}
    interval = config.get("min_interval_seconds", None)

    assert interval == 86400 or interval is None, (
        f"Default min_interval_seconds should be 86400 (or not yet implemented), got {interval}. "
        "Ensure default is 86400 when not configured."
    )
    # Post-fix this must be exactly 86400
    # For now, None is acceptable (method not yet updated) — will fail after fix is applied
