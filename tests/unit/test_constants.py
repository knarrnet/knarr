"""Tests for core protocol constants."""
import importlib
import os
import sys

import pytest

from knarr.core.constants import KNARR_MINT, KNARR_DECIMALS, KNARR_SYMBOL


def test_knarr_mint_is_mainnet_address():
    # Canonical mainnet $KNARR mint, Token-2022. Immutable once set.
    assert KNARR_MINT == "HgMcrNXKkvJb4KGVdW1yMYhPQsUUPY5mdobgzwkrZrW4"
    assert len(KNARR_MINT) == 44  # base58 Solana pubkey


def test_knarr_decimals_is_9():
    assert KNARR_DECIMALS == 9


def test_knarr_symbol_is_knarr():
    assert KNARR_SYMBOL == "KNARR"


# ---- A-08: cluster-aware mint resolution ----

def _reimport_constants():
    sys.modules.pop("knarr.core.constants", None)
    return importlib.import_module("knarr.core.constants")


def test_default_cluster_is_mainnet(monkeypatch):
    monkeypatch.delenv("KNARR_CLUSTER", raising=False)
    mod = _reimport_constants()
    assert mod.KNARR_CLUSTER == "mainnet"
    assert mod.KNARR_MINT == mod.KNARR_MINT_MAINNET


def test_devnet_cluster_selects_devnet_mint(monkeypatch):
    monkeypatch.setenv("KNARR_CLUSTER", "devnet")
    mod = _reimport_constants()
    assert mod.KNARR_CLUSTER == "devnet"
    # Devnet has no canonical mint — empty matches chain.py's token_mint=""
    # convention so downstream callers fail closed instead of using a
    # placeholder address as authoritative.
    assert mod.KNARR_MINT == ""
    assert mod.KNARR_MINT_DEVNET == ""
    assert mod.KNARR_MINT != mod.KNARR_MINT_MAINNET


def test_unknown_cluster_fails_loud(monkeypatch):
    monkeypatch.setenv("KNARR_CLUSTER", "testnet")
    sys.modules.pop("knarr.core.constants", None)
    with pytest.raises(ValueError, match="KNARR_CLUSTER"):
        importlib.import_module("knarr.core.constants")
    # leave mainnet reloaded for subsequent tests
    monkeypatch.delenv("KNARR_CLUSTER", raising=False)
    _reimport_constants()


def test_cluster_value_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("KNARR_CLUSTER", "MAINNET")
    mod = _reimport_constants()
    assert mod.KNARR_CLUSTER == "mainnet"
