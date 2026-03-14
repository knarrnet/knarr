"""BCW-IT-01: TransferEvent → credit ledger integration test.

Sprint v0.46.0, Section 9.1 (Forseti direct write).

Full in-process flow:
  TransferEvent (FINALIZED, payment_received)
    → _process_transfer → payment.finalized event emitted
    → _handle_payment_finalized → _credit_ledger
    → counterparty balance updated in SQLite ledger.
"""
import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from knarr.commerce.transfer_event import ConfirmationStatus, TransferEvent

# Import BCW handler under a unique module name to avoid sys.modules["handler"]
# collision with test_bcw.py and test_firewall.py.
_BCW_DIR = Path(__file__).parents[2] / "src" / "knarr" / "plugins" / "10-bcw"
_spec = importlib.util.spec_from_file_location("bcw_handler_it01_v46", _BCW_DIR / "handler.py")
_bcw_mod = importlib.util.module_from_spec(_spec)
sys.path.insert(0, str(_BCW_DIR))
try:
    _spec.loader.exec_module(_bcw_mod)
finally:
    sys.path.remove(str(_BCW_DIR))

BCWPlugin = _bcw_mod.BCWPlugin

# Deterministic counterparty identity: sha256(hex_bytes) = node_id
_PEER_PK = "aa" * 32                                                     # 64-char hex
_PEER_NODE_ID = hashlib.sha256(bytes.fromhex(_PEER_PK)).hexdigest()      # 64-char hex
_PEER_ADDR = "PeerSolanaAddr1111111111111111111111111111111"              # arbitrary watched address
_OWN_NODE_ID = hashlib.sha256(bytes.fromhex("cc" * 32)).hexdigest()      # provider's own node_id


def _make_ledger(db_path: str) -> None:
    """Create a minimal ledger table and pre-seed the counterparty public key."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ledger (
            peer_public_key TEXT PRIMARY KEY,
            balance         REAL    DEFAULT 0.0,
            tasks_provided  INTEGER DEFAULT 0,
            tasks_consumed  INTEGER DEFAULT 0,
            first_seen      REAL    DEFAULT 0.0,
            last_updated    REAL    DEFAULT 0.0
        )"""
    )
    conn.execute(
        "INSERT INTO ledger (peer_public_key, balance) VALUES (?, 0.0)",
        (_PEER_PK,),
    )
    conn.commit()
    conn.close()


def _make_plugin(tmp_path: Path, ledger_db: str, emitted: list) -> BCWPlugin:
    ctx = MagicMock()
    ctx.state_dir = tmp_path
    ctx.plugin_dir = tmp_path
    ctx.node_id = _OWN_NODE_ID
    ctx.storage_path = ledger_db
    ctx.economy_config = {}
    ctx.vault_get.return_value = None           # no master seed → _enabled=False
    ctx.emit_event.side_effect = lambda topic, **f: emitted.append({"event": topic, **f})
    plugin = BCWPlugin(ctx, config={})
    # Manually seed watchlist: counterparty Solana address → counterparty node_id.
    # In production this is populated by _handle_watch_request / _bootstrap_watchlist.
    plugin._store.upsert_watch(_PEER_NODE_ID, "solana-devnet", _PEER_ADDR)
    return plugin


def _read_balance(ledger_db: str) -> float:
    conn = sqlite3.connect(ledger_db)
    row = conn.execute(
        "SELECT balance FROM ledger WHERE peer_public_key = ?", (_PEER_PK,)
    ).fetchone()
    conn.close()
    assert row is not None, "Counterparty ledger entry not found"
    return row[0]


def test_finalized_payment_credits_ledger(tmp_path):
    """FINALIZED payment_received TransferEvent → counterparty ledger balance > 0."""
    ledger_db = str(tmp_path / "ledger.db")
    _make_ledger(ledger_db)
    emitted = []
    plugin = _make_plugin(tmp_path, ledger_db, emitted)

    transfer = TransferEvent(
        chain_id="solana-devnet",
        tx_hash="aa" * 32,
        tx_index=0,
        from_address="ExternalSender1111111111111111111111111111111",
        to_address=_PEER_ADDR,
        amount=1_000_000_000,    # 1.0 $KNARR (KNARR_DECIMALS = 9)
        denom="KNARR",
        decimals=9,
        confirmation=ConfirmationStatus.FINALIZED,
    )

    # Step 1: classify and emit payment.finalized.* event
    plugin._process_transfer(transfer)
    finalized = [e for e in emitted if e["event"].startswith("payment.finalized.")]
    assert finalized, "Expected payment.finalized.* event after FINALIZED transfer"

    # Step 2: credit path — resolve node_id, look up public_key, credit ledger
    plugin._handle_payment_finalized(finalized[0])

    # Step 3: balance must be positive (1.0 credit at default rate 1.0)
    balance = _read_balance(ledger_db)
    assert balance > 0.0, f"Expected positive balance after BCW credit, got {balance}"


def test_included_transfer_does_not_credit(tmp_path):
    """INCLUDED (not FINALIZED) payment_received → no credit written."""
    ledger_db = str(tmp_path / "ledger.db")
    _make_ledger(ledger_db)
    emitted = []
    plugin = _make_plugin(tmp_path, ledger_db, emitted)

    transfer = TransferEvent(
        chain_id="solana-devnet",
        tx_hash="bb" * 32,
        tx_index=0,
        from_address="ExternalSender1111111111111111111111111111111",
        to_address=_PEER_ADDR,
        amount=1_000_000_000,
        denom="KNARR",
        decimals=9,
        confirmation=ConfirmationStatus.INCLUDED,   # not FINALIZED
    )

    plugin._process_transfer(transfer)
    finalized = [e for e in emitted if e["event"].startswith("payment.finalized.")]
    assert not finalized, "INCLUDED transfer must not emit payment.finalized.*"
    assert _read_balance(ledger_db) == 0.0, "INCLUDED transfer must not credit ledger"
