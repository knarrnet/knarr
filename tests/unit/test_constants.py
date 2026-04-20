"""Tests for core protocol constants."""
from knarr.core.constants import KNARR_MINT, KNARR_DECIMALS, KNARR_SYMBOL


def test_knarr_mint_is_mainnet_address():
    # Canonical mainnet $KNARR mint, Token-2022. Immutable once set.
    assert KNARR_MINT == "HgMcrNXKkvJb4KGVdW1yMYhPQsUUPY5mdobgzwkrZrW4"
    assert len(KNARR_MINT) == 44  # base58 Solana pubkey


def test_knarr_decimals_is_9():
    assert KNARR_DECIMALS == 9


def test_knarr_symbol_is_knarr():
    assert KNARR_SYMBOL == "KNARR"
