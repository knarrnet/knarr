import importlib.util
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

from knarr.commerce.transfer_event import ConfirmationStatus, TransferEvent


BASE_DIR = Path(__file__).resolve().parent.parent.parent
PLUGIN_DIR = BASE_DIR / "src" / "knarr" / "plugins" / "10-bcw"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

spec = importlib.util.spec_from_file_location("bcw_handler_a09_v58", PLUGIN_DIR / "handler.py")
handler = importlib.util.module_from_spec(spec)
sys.modules["bcw_handler_a09_v58"] = handler
spec.loader.exec_module(handler)


KNARR_MINT = "KnarrMint11111111111111111111111111111111111"
WRONG_MINT = "WrongMint11111111111111111111111111111111111"
SELF_ADDR = "SelfDepositAddress111111111111111111111111111"
EXTERNAL_ADDR = "ExternalSender11111111111111111111111111111"


def _make_plugin(tmp_path):
    ctx = MagicMock()
    ctx.node_id = "aa" * 32
    ctx.emit_event = MagicMock()
    ctx.sign_document.side_effect = lambda doc: dict(doc)
    ctx.log = MagicMock()

    plugin = handler.BCWPlugin.__new__(handler.BCWPlugin)
    plugin._ctx = ctx
    plugin._config = {"knarr_mint": KNARR_MINT}
    plugin._debug = False
    plugin._store = handler.WatchStore(tmp_path / "bcw.sqlite3")
    plugin._store.upsert_watch("bb" * 32, "solana-mainnet", SELF_ADDR)
    plugin._self_owned_addresses = {SELF_ADDR}
    return plugin, ctx


def _transfer(*, mint, tx_hash="tx-a09", confirmation=ConfirmationStatus.FINALIZED):
    return TransferEvent(
        chain_id="solana-mainnet",
        tx_hash=tx_hash,
        tx_index=0,
        from_address=EXTERNAL_ADDR,
        to_address=SELF_ADDR,
        amount=50_000,
        denom="KNARR" if mint else "SOL",
        decimals=9,
        confirmation=confirmation,
        mint_address=mint,
    )


def _receipt_count(plugin):
    conn = sqlite3.connect(plugin._store._db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM bcw_receipts").fetchone()[0]
    finally:
        conn.close()


def _topics(ctx):
    return [call.args[0] for call in ctx.emit_event.call_args_list]


def test_wrong_mint_ingestion_writes_no_receipts_and_emits_no_events(tmp_path):
    plugin, ctx = _make_plugin(tmp_path)

    plugin._process_transfer(_transfer(mint=WRONG_MINT))

    assert _receipt_count(plugin) == 0
    ctx.emit_event.assert_not_called()


def test_correct_mint_ingestion_writes_receipts_and_emits_payment_events(tmp_path):
    plugin, ctx = _make_plugin(tmp_path)

    plugin._process_transfer(_transfer(mint=KNARR_MINT))

    assert _receipt_count(plugin) == 2
    assert "payment.received.solana" in _topics(ctx)
    assert "payment.finalized.solana" in _topics(ctx)


def test_native_sol_empty_mint_passes_through_unchanged(tmp_path):
    plugin, ctx = _make_plugin(tmp_path)

    plugin._process_transfer(_transfer(mint="", tx_hash="native-sol"))

    assert _receipt_count(plugin) == 2
    finalized = [
        call.kwargs
        for call in ctx.emit_event.call_args_list
        if call.args[0] == "payment.finalized.solana"
    ]
    assert finalized
    assert finalized[0]["mint"] == ""


def test_payment_finalized_wrong_mint_guard_remains_in_place(tmp_path):
    plugin, ctx = _make_plugin(tmp_path)

    plugin._handle_payment_finalized(
        {
            "event": "payment.finalized.solana",
            "mint": WRONG_MINT,
            "tx_hash": "direct-event",
        }
    )

    ctx.log.warning.assert_called_once_with(
        "BCW: rejected payment with wrong mint %s",
        WRONG_MINT,
    )
