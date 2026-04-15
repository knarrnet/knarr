"""A3 contract test: BCW must validate mint address before crediting payment.

ADV-7 (Tier 1 — payment fraud): _handle_payment_finalized accepts any SPL token
as a KNARR payment. An attacker mints a lookalike token, sends it as settlement —
BCW credits the ledger as if it were KNARR.

FIX LOCATION: plugins/10-bcw/handler.py — _handle_payment_finalized()
After resolving assigned_node_id and before calling _credit_ledger:
    mint = event.get("mint") or event.get("token_mint") or ""
    if mint != self._knarr_mint():
        self._log_warning("BCW: rejected payment with wrong mint %s", mint)
        return

CONTRACT:
- _handle_payment_finalized with a wrong/empty mint address must NOT call
  _credit_ledger (no ledger modification).
- _handle_payment_finalized with the correct KNARR mint must call _credit_ledger.
- The configured KNARR mint is sourced from knarr.core.constants.KNARR_MINT
  or from plugin config ["knarr_mint"].
"""
import pytest
from unittest.mock import MagicMock, patch, call


def _make_bcw_handler(knarr_mint="So11111111111111111111111111111111111111112"):
    """Build a minimal BCWHandler with a mocked store and watchlist."""
    import sys, pathlib, importlib.util
    plugin_dir = pathlib.Path(__file__).parents[2] / "src" / "knarr" / "plugins" / "10-bcw"
    # sys.path.insert needed so BCW's `from solana import ...` fallback finds the local solana.py
    # (not the PyPI solana package). spec_from_file_location with a unique name avoids the
    # sys.modules['handler'] collision with 01-firewall when tests run in the same session.
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))
    spec = importlib.util.spec_from_file_location("bcw_handler", plugin_dir / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bcw_handler"] = mod
    spec.loader.exec_module(mod)
    _BCW = mod.BCWPlugin

    ctx = MagicMock()
    ctx.node_id = "aa" * 32
    ctx.storage_path = None
    ctx.state_dir = None
    ctx.plugin_dir = MagicMock()
    ctx.plugin_dir.__truediv__ = lambda self, other: MagicMock()
    ctx.subscribe_events = None

    config = {
        "enabled": True,
        "chains": [],
        "knarr_mint": knarr_mint,
    }

    handler = _BCW.__new__(_BCW)
    handler._ctx = ctx
    handler._config = config
    handler._enabled = True
    handler._log = MagicMock()
    handler._solana_modules = {}
    handler._store = MagicMock()
    handler._master_seed = b"\x00" * 32
    handler._sub = None
    handler._payment_sub = None
    handler._credit_ledger = MagicMock()
    handler._resolve_peer_public_key = MagicMock(return_value="bb" * 32)
    handler._economy_config = MagicMock(return_value={"conversion_rate": 1.0})
    return handler


def _payment_event(to_address="wallet123", amount=10.0, mint=None):
    event = {
        "event": "payment.finalized.solana-devnet",
        "to_address": to_address,
        "amount": amount,
    }
    if mint is not None:
        event["mint"] = mint
    return event


KNARR_MINT = "So11111111111111111111111111111111111111112"
FAKE_MINT  = "FakeMint111111111111111111111111111111111111"


@pytest.fixture
def bcw():
    handler = _make_bcw_handler(knarr_mint=KNARR_MINT)
    handler._store.get_node_id_for_address = MagicMock(return_value="cc" * 32)
    return handler


def test_wrong_mint_does_not_credit(bcw):
    """Payment with wrong mint must not credit the ledger."""
    event = _payment_event(mint=FAKE_MINT)
    bcw._handle_payment_finalized(event)
    bcw._credit_ledger.assert_not_called(), (
        "BCW credited ledger for a non-KNARR token. "
        "Fix: validate mint address in _handle_payment_finalized before _credit_ledger."
    )


def test_missing_mint_does_not_credit(bcw):
    """Payment with no mint field must not credit the ledger."""
    event = _payment_event()  # no mint key
    bcw._handle_payment_finalized(event)
    bcw._credit_ledger.assert_not_called(), (
        "BCW credited ledger for a payment with no mint field. "
        "Fix: require mint field; missing mint = rejected."
    )


## test_correct_mint_credits_ledger removed — _handle_payment_finalized is now a stub
## (credit logic moved to bcw_credit.py).  Positive-path credit is tested in
## test_cr01_bcw_credit_v50.py::test_valid_payment_credits_ledger.


def test_empty_string_mint_does_not_credit(bcw):
    """Empty string mint must not credit."""
    event = _payment_event(mint="")
    bcw._handle_payment_finalized(event)
    bcw._credit_ledger.assert_not_called()
