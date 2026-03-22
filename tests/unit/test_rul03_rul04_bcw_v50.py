import asyncio
import importlib as py_importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from knarr.commerce.transfer_event import ConfirmationStatus, TransferEvent

BASE_DIR = Path(__file__).resolve().parent.parent.parent
PLUGIN_DIR = BASE_DIR / "src" / "knarr" / "plugins" / "10-bcw"
sys.path.insert(0, str(BASE_DIR / "src"))
sys.path.insert(0, str(PLUGIN_DIR))

import handler as bcw_handler  # noqa: E402
import solana as bcw_solana  # noqa: E402


class StubSubscriber:
    def __init__(self):
        self._events = []

    def push(self, event):
        self._events.append(event)

    def poll(self):
        events = list(self._events)
        self._events.clear()
        return events


class FakeManager:
    instances = []

    def __init__(self, chain_id, ws_url):
        self.chain_id = chain_id
        self._ws_url = ws_url
        self._connected = True
        self.on_notification = None
        self._on_reconnect_callback = None
        self.unsubscribe_calls = []
        FakeManager.instances.append(self)

    async def _reconnect_loop(self):
        return None

    async def subscribe_account(self, address, node_id, correlation_id):
        return None

    async def subscribe_signature(self, tx_hash, node_id, correlation_id):
        return None

    async def unsubscribe_all_for(self, node_id, chain_id):
        self.unsubscribe_calls.append((node_id, chain_id))

    async def disconnect(self):
        return None


def _make_plugin(tmp_path, monkeypatch):
    FakeManager.instances.clear()
    real_import_module = py_importlib.import_module
    monkeypatch.setattr(
        bcw_handler.importlib,
        "import_module",
        lambda name: MagicMock() if name == "websockets" else real_import_module(name),
    )
    monkeypatch.setattr(bcw_handler, "SolanaSubscriptionManager", FakeManager)
    clock = {"now": 1000.0}
    monkeypatch.setattr(bcw_handler.time, "time", lambda: clock["now"])

    emitted = []
    subscriber = StubSubscriber()
    ctx = MagicMock()
    ctx.plugin_dir = tmp_path
    ctx.state_dir = tmp_path
    ctx.node_id = "a" * 64
    ctx.subscribe_events.side_effect = lambda *patterns: subscriber
    ctx.emit_event.side_effect = lambda event, **fields: emitted.append({"event": event, **fields})
    ctx.log = MagicMock()
    ctx.sign_document.side_effect = lambda doc: doc
    ctx.vault_get.side_effect = lambda *args: "11" * 32 if args and args[-1] == "bcw_master_seed" else None
    ctx.economy_config = {}

    plugin = bcw_handler.BCWPlugin(
        ctx,
        {
            "enabled": True,
            "poll_interval_seconds": 10,
            "chains": [
                {
                    "chain_id": "solana-devnet",
                    "rpc_url": "https://rpc.example",
                    "token_mints": {"KNARR": "So11111111111111111111111111111111111111112"},
                }
            ],
        },
    )
    watcher = plugin._solana_modules["solana-devnet"]
    watcher.poll_address = AsyncMock(return_value=bcw_solana.PollResult([], None, True))
    plugin._check_sol_balance = AsyncMock()
    manager = FakeManager.instances[0]
    return plugin, watcher, manager, subscriber, emitted, clock


@pytest.mark.asyncio
async def test_ws_and_http_gap_recovery_emit_same_payment_finalized_shape(tmp_path, monkeypatch):
    plugin_ws, watcher_ws, _manager_ws, subscriber_ws, emitted_ws, _clock_ws = _make_plugin(tmp_path / "ws", monkeypatch)
    subscriber_ws.push({"event": "bcw.watch_request", "node_id": "b" * 64, "chain_id": "solana-devnet", "correlation_id": "corr"})
    await plugin_ws.on_tick([], None)
    await asyncio.sleep(0)
    transfer_ws = TransferEvent(
        chain_id="solana-devnet",
        tx_hash="sig-shape",
        tx_index=0,
        from_address="From11111111111111111111111111111111111",
        to_address=plugin_ws._store.get_address("b" * 64, "solana-devnet"),
        amount=5,
        denom="KNARR",
        decimals=9,
        confirmation=ConfirmationStatus.FINALIZED,
    )
    watcher_ws.poll_address = AsyncMock(return_value=bcw_solana.PollResult([transfer_ws], "sig-shape", True))
    plugin_ws._on_ws_notification(
        {"type": "account", "node_id": "b" * 64, "chain_id": "solana-devnet", "correlation_id": "corr"},
        {"value": {}},
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    finalized_ws = next(event for event in emitted_ws if event["event"].startswith("payment.finalized."))

    plugin_http, watcher_http, manager_http, subscriber_http, emitted_http, clock_http = _make_plugin(tmp_path / "http", monkeypatch)
    subscriber_http.push({"event": "bcw.watch_request", "node_id": "b" * 64, "chain_id": "solana-devnet", "correlation_id": "corr"})
    await plugin_http.on_tick([], None)
    await asyncio.sleep(0)
    manager_http._connected = False
    transfer_http = TransferEvent(
        chain_id="solana-devnet",
        tx_hash="sig-shape",
        tx_index=0,
        from_address="From11111111111111111111111111111111111",
        to_address=plugin_http._store.get_address("b" * 64, "solana-devnet"),
        amount=5,
        denom="KNARR",
        decimals=9,
        confirmation=ConfirmationStatus.FINALIZED,
    )
    watcher_http.poll_address = AsyncMock(return_value=bcw_solana.PollResult([transfer_http], "sig-shape", True))
    clock_http["now"] = 1035.0
    await plugin_http._poll_gap_recovery("solana-devnet")
    finalized_http = next(event for event in emitted_http if event["event"].startswith("payment.finalized."))

    assert set(finalized_ws.keys()) == set(finalized_http.keys())
    for key in ("event", "chain_id", "tx_hash", "amount", "denom", "decimals", "dedup_key", "correlation_id"):
        assert finalized_ws[key] == finalized_http[key]


@pytest.mark.asyncio
async def test_peer_removed_auto_unwatches_only_matching_peer(tmp_path, monkeypatch):
    plugin, _watcher, manager, subscriber, _emitted, _clock = _make_plugin(tmp_path, monkeypatch)
    subscriber.push({"event": "bcw.watch_request", "node_id": "b" * 64, "chain_id": "solana-devnet"})
    subscriber.push({"event": "bcw.watch_request", "node_id": "c" * 64, "chain_id": "solana-devnet"})
    await plugin.on_tick([], None)
    await asyncio.sleep(0)

    subscriber.push({"event": "peer.removed", "node_id": "b" * 64})
    await plugin.on_tick([], None)
    await asyncio.sleep(0)

    remaining = {(row["node_id"], row["chain_id"]) for row in plugin._store.list_watches()}
    assert ("b" * 64, "solana-devnet") not in remaining
    assert ("c" * 64, "solana-devnet") in remaining
    assert ("b" * 64, "solana-devnet") in manager.unsubscribe_calls
