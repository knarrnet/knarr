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


## test_lamports_normalized_to_tokens (4 parametrized cases) removed —
## _handle_payment_finalized is now a stub (credit logic moved to bcw_credit.py).
## Lamport normalization is tested in test_cr01_bcw_credit_v50.py via the
## bcw_credit.make_payment_finalized_handler path (decimals field handling).


def test_knarr_decimals_is_9():
    """Normalization depends on KNARR_DECIMALS == 9."""
    assert KNARR_DECIMALS == 9, (
        f"KNARR_DECIMALS must be 9 for lamport normalization. Got {KNARR_DECIMALS}."
    )
