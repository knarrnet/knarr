"""Tests for core protocol constants."""
from knarr.core.constants import KNARR_MINT, KNARR_DECIMALS, KNARR_SYMBOL


def test_knarr_mint_is_string():
    assert isinstance(KNARR_MINT, str)


def test_knarr_decimals_is_9():
    assert KNARR_DECIMALS == 9


def test_knarr_symbol_is_knarr():
    assert KNARR_SYMBOL == "KNARR"
