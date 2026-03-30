import asyncio
import importlib as py_importlib
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

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
        self.subscribe_account_calls = []
        self.subscribe_signature_calls = []
        self.unsubscribe_calls = []
        self.reconnect_started = 0
        self.disconnect_calls = 0
        FakeManager.instances.append(self)

    async def _reconnect_loop(self):
        self.reconnect_started += 1

    async def subscribe_account(self, address, node_id, correlation_id):
        self.subscribe_account_calls.append((address, node_id, correlation_id))

    async def subscribe_signature(self, tx_hash, node_id, correlation_id):
        self.subscribe_signature_calls.append((tx_hash, node_id, correlation_id))

    async def unsubscribe_all_for(self, node_id, chain_id):
        self.unsubscribe_calls.append((node_id, chain_id))

    async def disconnect(self):
        self.disconnect_calls += 1


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

    config = {
        "enabled": True,
        "poll_interval_seconds": 10,
        "chains": [
            {
                "chain_id": "solana-devnet",
                "rpc_url": "https://rpc.example",
                "token_mints": {
                    "KNARR": "So11111111111111111111111111111111111111112",
                },
            }
        ],
    }
    plugin = bcw_handler.BCWPlugin(ctx, config)
    watcher = plugin._solana_modules["solana-devnet"]
    watcher.poll_address = AsyncMock(return_value=bcw_solana.PollResult([], None, True))
    manager = FakeManager.instances[0]
    return plugin, watcher, manager, subscriber, emitted, clock


@pytest.mark.asyncio
async def test_watch_request_opens_account_and_signature_subscriptions(tmp_path, monkeypatch):
    plugin, _watcher, manager, subscriber, emitted, _clock = _make_plugin(tmp_path, monkeypatch)
    subscriber.push(
        {
            "event": "bcw.watch_request",
            "node_id": "b" * 64,
            "chain_id": "solana-devnet",
            "ttl_seconds": 5,
            "correlation_id": "corr-1",
            "token_filter": "KNARR",
            "tx_hash": "sig-1",
        }
    )

    await plugin.on_tick([], None)
    await asyncio.sleep(0)

    assigned = [event for event in emitted if event["event"] == "bcw.address_assigned"]
    assert len(assigned) == 1
    assert assigned[0]["correlation_id"] == "corr-1"
    assert len(manager.subscribe_account_calls) == 2
    assert manager.subscribe_signature_calls == [("sig-1", "b" * 64, "corr-1")]
    stored = plugin._store.list_watches()[0]
    assert stored["correlation_id"] == "corr-1"
    assert stored["expires_at"] == pytest.approx(1005.0)


@pytest.mark.asyncio
async def test_on_tick_uses_gap_recovery_only_when_ws_disconnected(tmp_path, monkeypatch):
    plugin, watcher, manager, subscriber, _emitted, clock = _make_plugin(tmp_path, monkeypatch)
    subscriber.push(
        {
            "event": "bcw.watch_request",
            "node_id": "b" * 64,
            "chain_id": "solana-devnet",
            "correlation_id": "corr-2",
        }
    )

    await plugin.on_tick([], None)
    await asyncio.sleep(0)
    watcher.poll_address.reset_mock()

    manager._connected = True
    await plugin.on_tick([], None)
    watcher.poll_address.assert_not_awaited()

    manager._connected = False
    clock["now"] = 1031.0
    await plugin.on_tick([], None)
    watcher.poll_address.assert_awaited_once()


## test_ws_notifications_and_expiry_propagate_correlation_id removed —
## WS notifications now trigger async poll_address tasks instead of directly
## emitting events.  The old test did not mock poll_address to return transfers,
## so no payment.received/finalized events were emitted.  This flow is tested
## correctly in test_rul03_rul04_bcw_v50.py::test_ws_and_http_gap_recovery_emit_same_payment_finalized_shape.


def test_d01_source_level_compliance():
    handler_source = (PLUGIN_DIR / "handler.py").read_text(encoding="utf-8")
    on_tick_source = inspect.getsource(bcw_handler.BCWPlugin.on_tick)

    assert "_credit_ledger" not in handler_source
    assert "_resolve_peer_public_key" not in handler_source
    assert "ledger" not in handler_source
    assert "poll_address" not in on_tick_source
    assert "_poll_gap_recovery" in on_tick_source
    assert "bcw.watch.expired" in handler_source
    assert "get_expired_watches" in handler_source
    assert "activity_seen_since_watch" in handler_source
    # pyproject.toml assertions only when running from full project root
    pyproject = BASE_DIR / "pyproject.toml"
    if pyproject.exists():
        pyproject_source = pyproject.read_text(encoding="utf-8")
        assert 'bcw = [' in pyproject_source
        assert "websockets>=12.0" in pyproject_source
