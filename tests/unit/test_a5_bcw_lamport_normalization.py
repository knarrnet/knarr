"""A5 contract test: BCW lamport normalization.

ADV-6: SolanaWatcher emits raw lamport amounts. Downstream _handle_payment_finalized
passes event["amount"] directly to token_to_credits(). This is off by 1e9 —
1 SOL = 1,000,000,000 lamports, but the code treats it as 1,000,000,000 tokens.

FIX LOCATION: plugins/10-bcw/handler.py
At the point where amount is read from the payment.finalized event, normalize:
    raw_amount = float(event.get("amount", 0.0))
    amount = raw_amount / (10 ** KNARR_DECIMALS)  # lamports → whole tokens

Or at the SolanaWatcher emission point — divide lamports by 10^9 before emitting.

CONTRACT:
- A payment.finalized event with amount=1_000_000_000 (1 SOL in lamports) must
  result in token_to_credits being called with ~1.0 (not 1e9).
- A payment.finalized event with amount=500_000_000 must result in ~0.5 tokens.
- KNARR_DECIMALS is 9 (from knarr.core.constants).
"""
import pytest
from unittest.mock import MagicMock, patch, call
from knarr.core.constants import KNARR_DECIMALS


KNARR_MINT = "So11111111111111111111111111111111111111112"
PEER_KEY = "ab" * 32


def _make_bcw_with_credit_capture():
    """Build BCWHandler stub that captures amount passed to _credit_ledger."""
    import sys, pathlib
    plugin_path = str(pathlib.Path(__file__).parents[4] / "knarr.clean" / "src" / "knarr" / "plugins" / "10-bcw")
    if plugin_path not in sys.path:
        sys.path.insert(0, plugin_path)
    from handler import BCWHandler

    ctx = MagicMock()
    ctx.node_id = "cc" * 32
    ctx.storage_path = None
    ctx.state_dir = None
    ctx.plugin_dir = MagicMock()
    ctx.plugin_dir.__truediv__ = lambda self, other: MagicMock()
    ctx.subscribe_events = None

    handler = BCWHandler.__new__(BCWHandler)
    handler._ctx = ctx
    handler._config = {"enabled": True, "chains": [], "knarr_mint": KNARR_MINT}
    handler._enabled = True
    handler._log = MagicMock()
    handler._solana_modules = {}
    handler._store = MagicMock()
    handler._store.get_node_id_for_address = MagicMock(return_value=PEER_KEY)
    handler._master_seed = b"\x00" * 32
    handler._sub = None
    handler._payment_sub = None
    handler._resolve_peer_public_key = MagicMock(return_value="bb" * 32)
    handler._economy_config = MagicMock(return_value={"conversion_rate": 1.0})

    credited_amounts = []
    handler._credit_ledger = MagicMock(side_effect=lambda peer_key, credits: credited_amounts.append(credits))
    handler._credited_amounts = credited_amounts
    return handler


def _finalized_event(amount_lamports, mint=KNARR_MINT):
    return {
        "event": "payment.finalized.solana-devnet",
        "to_address": "wallet123",
        "amount": amount_lamports,
        "mint": mint,
    }


@pytest.mark.parametrize("lamports,expected_tokens", [
    (1_000_000_000, 1.0),
    (500_000_000, 0.5),
    (100_000_000, 0.1),
    (10_000_000_000, 10.0),
])
def test_lamports_normalized_to_tokens(lamports, expected_tokens):
    """BCW must normalize lamport amounts to whole token amounts."""
    handler = _make_bcw_with_credit_capture()
    handler._handle_payment_finalized(_finalized_event(lamports))

    assert len(handler._credited_amounts) == 1, "Expected _credit_ledger to be called once."
    # token_to_credits applies conversion_rate=1.0, so credited == tokens
    credited = handler._credited_amounts[0]
    assert abs(credited - expected_tokens) < 0.001, (
        f"amount={lamports} lamports should credit ~{expected_tokens} tokens, "
        f"but credited {credited}. "
        f"Fix: divide by 10**KNARR_DECIMALS ({10**KNARR_DECIMALS}) before token_to_credits."
    )


def test_knarr_decimals_is_9():
    """Normalization depends on KNARR_DECIMALS == 9."""
    assert KNARR_DECIMALS == 9, (
        f"KNARR_DECIMALS must be 9 for lamport normalization. Got {KNARR_DECIMALS}."
    )
