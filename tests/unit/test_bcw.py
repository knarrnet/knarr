import sys
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

import knarr  # noqa: E402

knarr.__path__.insert(0, str(BASE_DIR / "src" / "knarr"))

import knarr.commerce  # noqa: E402

knarr.commerce.__path__.insert(0, str(BASE_DIR / "src" / "knarr" / "commerce"))

plugin_path = BASE_DIR / "src" / "knarr" / "plugins" / "10-bcw"
sys.path.insert(0, str(plugin_path))

import handler  # noqa: E402
import solana  # noqa: E402
from knarr.commerce.transfer_event import ConfirmationStatus, TransferEvent  # noqa: E402


class StubSubscriber:
    def __init__(self):
        self._events = []

    def push(self, event: dict) -> None:
        self._events.append(event)

    def poll(self) -> list[dict]:
        events = list(self._events)
        self._events.clear()
        return events


@pytest.fixture
def config():
    return {
        "enabled": True,
        "poll_interval_seconds": 10,
        "chains": [
            {
                "chain_id": "solana-mainnet",
                "rpc_url": "http://mock-rpc",
                "tokens": ["KNARR"],
                "token_mints": {"KNARR": "KNRRmint111111111111111111111111111111111111"},
                "commitment": "finalized",
                "min_amount_lamports": 10_000,
            }
        ],
    }


@pytest.fixture
def mock_ctx(tmp_path):
    sub = StubSubscriber()
    ctx = MagicMock()
    ctx.plugin_dir = tmp_path
    ctx.node_id = "a" * 64
    ctx.subscribe_events.return_value = sub
    ctx.get_peers.return_value = []
    ctx.emit_event = MagicMock()
    ctx.log = MagicMock()
    ctx.sign_document.side_effect = lambda doc: {**doc, "proof": {"type": "test-proof"}}

    seed_hex = "11" * 32

    def vault_get(*args):
        if args and args[-1] == "bcw_master_seed":
            return seed_hex
        return None

    ctx.vault_get.side_effect = vault_get
    ctx._test_sub = sub
    return ctx


@pytest.fixture
def plugin(mock_ctx, config):
    return handler.BCWPlugin(mock_ctx, config)


def _self_addresses(plugin_obj) -> list[str]:
    rows = plugin_obj._store.list_watches()
    return [row["address"] for row in rows if row["address"]]


def test_address_derivation_deterministic():
    seed = b"\x01" * 32
    node_id = "a" * 64
    addr1 = handler.derive_counterparty_address(seed, node_id, "solana-mainnet")
    addr2 = handler.derive_counterparty_address(seed, node_id, "solana-mainnet")
    assert addr1 == addr2


def test_address_derivation_different_node_ids():
    seed = b"\x02" * 32
    addr1 = handler.derive_counterparty_address(seed, "a" * 64, "solana-mainnet")
    addr2 = handler.derive_counterparty_address(seed, "b" * 64, "solana-mainnet")
    assert addr1 != addr2


def test_address_derivation_rejects_short_node_id():
    with pytest.raises(ValueError, match="64 hex chars"):
        handler.derive_counterparty_address(b"\x03" * 32, "short", "solana-mainnet")


def test_classification_inbound_to_payment_received(plugin):
    self_addr = _self_addresses(plugin)[0]
    event = TransferEvent(
        chain_id="solana-mainnet",
        tx_hash="tx-in",
        tx_index=0,
        from_address="External11111111111111111111111111111111111",
        to_address=self_addr,
        amount=50_000,
        denom="SOL",
        decimals=9,
        confirmation=ConfirmationStatus.FINALIZED,
    )
    assert plugin._classify_transfer(event) == "payment_received"


def test_classification_self_to_self_to_wallet_transfer(plugin):
    a1, a2 = _self_addresses(plugin)[:2]
    event = TransferEvent(
        chain_id="solana-mainnet",
        tx_hash="tx-self",
        tx_index=1,
        from_address=a1,
        to_address=a2,
        amount=80_000,
        denom="SOL",
        decimals=9,
        confirmation=ConfirmationStatus.FINALIZED,
    )
    assert plugin._classify_transfer(event) == "wallet_transfer"


def test_classification_outbound_known_to_payment_executed(plugin):
    self_addr = _self_addresses(plugin)[0]
    known_wallet = "KnownWallet11111111111111111111111111111111"
    plugin._known_wallet_addresses = {known_wallet}
    event = TransferEvent(
        chain_id="solana-mainnet",
        tx_hash="tx-known",
        tx_index=2,
        from_address=self_addr,
        to_address=known_wallet,
        amount=100_000,
        denom="SOL",
        decimals=9,
        confirmation=ConfirmationStatus.FINALIZED,
    )
    assert plugin._classify_transfer(event) == "payment_executed"


def test_classification_outbound_unknown_to_wallet_withdrawal(plugin):
    self_addr = _self_addresses(plugin)[0]
    event = TransferEvent(
        chain_id="solana-mainnet",
        tx_hash="tx-unknown",
        tx_index=3,
        from_address=self_addr,
        to_address="UnknownWallet1111111111111111111111111111111",
        amount=100_000,
        denom="SOL",
        decimals=9,
        confirmation=ConfirmationStatus.FINALIZED,
    )
    assert plugin._classify_transfer(event) == "wallet_withdrawal"


@pytest.mark.asyncio
async def test_dedup_second_identical_transfer_ignored(plugin, mock_ctx):
    self_addr = _self_addresses(plugin)[0]
    event = TransferEvent(
        chain_id="solana-mainnet",
        tx_hash="sig-dedup",
        tx_index=0,
        from_address="FromExternal11111111111111111111111111111",
        to_address=self_addr,
        amount=120_000,
        denom="SOL",
        decimals=9,
        confirmation=ConfirmationStatus.FINALIZED,
    )

    async def fake_poll(address, last_signature):
        if address == self_addr:
            return solana.PollResult(events=[event], latest_signature="sig-dedup", rpc_ok=True)
        return solana.PollResult(events=[], latest_signature=last_signature, rpc_ok=True)

    watcher = plugin._solana_modules["solana-mainnet"]
    watcher.poll_address = AsyncMock(side_effect=fake_poll)

    await plugin.on_tick([], None)
    await plugin.on_tick([], None)

    topics = [call.args[0] for call in mock_ctx.emit_event.call_args_list]
    assert topics.count("payment.received.solana") == 1


@pytest.mark.asyncio
async def test_dust_filter_sub_threshold_not_emitted(config):
    watcher = solana.SolanaWatcher("solana-mainnet", config["chains"][0])

    async def fake_call(method, params):
        if method == "getSignaturesForAddress":
            return {"result": [{"signature": "sig-dust"}]}
        if method == "getTransaction":
            return {
                "result": {
                    "slot": 1,
                    "blockTime": 2,
                    "transaction": {
                        "message": {
                            "instructions": [
                                {
                                    "program": "system",
                                    "parsed": {
                                        "type": "transfer",
                                        "info": {
                                            "source": "src",
                                            "destination": "watched",
                                            "lamports": 500,
                                        },
                                    },
                                }
                            ]
                        }
                    },
                    "meta": {"innerInstructions": []},
                }
            }
        raise AssertionError(f"unexpected method: {method}")

    watcher._call_rpc = fake_call
    result = await watcher.poll_address("watched", None)
    assert result.events == []


@pytest.mark.asyncio
async def test_cursor_last_signature_updated_after_poll(plugin):
    watcher = plugin._solana_modules["solana-mainnet"]
    watcher.poll_address = AsyncMock(
        return_value=solana.PollResult(events=[], latest_signature="sig-cursor", rpc_ok=True)
    )

    await plugin.on_tick([], None)

    rows = plugin._store.list_watches()
    assert all(row["last_signature"] == "sig-cursor" for row in rows)


@pytest.mark.asyncio
async def test_watch_request_emits_address_assigned(plugin, mock_ctx):
    mock_ctx._test_sub.push(
        {
            "event": "bcw.watch_request",
            "node_id": "b" * 64,
            "chain_id": "solana-mainnet",
        }
    )

    watcher = plugin._solana_modules["solana-mainnet"]
    watcher.poll_address = AsyncMock(
        return_value=solana.PollResult(events=[], latest_signature=None, rpc_ok=True)
    )

    await plugin.on_tick([], None)

    mock_ctx.emit_event.assert_any_call(
        "bcw.address_assigned",
        node_id="b" * 64,
        chain_id="solana-mainnet",
        address=ANY,
    )


@pytest.mark.asyncio
async def test_rpc_error_logged_not_crashed(plugin, mock_ctx):
    watcher = plugin._solana_modules["solana-mainnet"]
    watcher.poll_address = AsyncMock(side_effect=RuntimeError("rpc timeout"))

    await plugin.on_tick([], None)
    assert mock_ctx.log.warning.called


@pytest.mark.asyncio
async def test_missing_vault_seed_disables_gracefully(tmp_path, config):
    ctx = MagicMock()
    ctx.plugin_dir = tmp_path
    ctx.node_id = "c" * 64
    ctx.subscribe_events.return_value = StubSubscriber()
    ctx.vault_get.return_value = None
    ctx.get_peers.return_value = []
    ctx.emit_event = MagicMock()
    ctx.log = MagicMock()

    bcw = handler.BCWPlugin(ctx, config)
    assert bcw._enabled is False
    await bcw.on_tick([], None)


@pytest.mark.asyncio
async def test_nan_inf_guard_on_amount_parsing(config):
    watcher = solana.SolanaWatcher("solana-mainnet", config["chains"][0])

    assert watcher._parse_positive_amount(float("nan")) is None
    assert watcher._parse_positive_amount(float("inf")) is None
    assert watcher._parse_positive_amount("-inf") is None

    async def fake_call(method, params):
        if method == "getSignaturesForAddress":
            return {"result": [{"signature": "sig-nan"}]}
        if method == "getTransaction":
            return {
                "result": {
                    "slot": 1,
                    "blockTime": 2,
                    "transaction": {
                        "message": {
                            "instructions": [
                                {
                                    "program": "system",
                                    "parsed": {
                                        "type": "transfer",
                                        "info": {
                                            "source": "src",
                                            "destination": "watched",
                                            "lamports": "NaN",
                                        },
                                    },
                                }
                            ]
                        }
                    },
                    "meta": {"innerInstructions": []},
                }
            }
        raise AssertionError(f"unexpected method: {method}")

    watcher._call_rpc = fake_call
    result = await watcher.poll_address("watched", None)
    assert result.events == []
