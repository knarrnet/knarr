"""Tests for core protocol constants."""
import importlib
import os
import sys

import pytest

from knarr.core.constants import KNARR_MINT, KNARR_DECIMALS, KNARR_SYMBOL


def test_knarr_mint_mainnet_pending():
    # Mainnet KNR mint address is pending — empty until new mint is executed.
    assert KNARR_MINT == ""


def test_knarr_decimals_is_9():
    assert KNARR_DECIMALS == 9


def test_knarr_symbol_is_knr():
    assert KNARR_SYMBOL == "KNR"


# ---- A-08: cluster-aware mint resolution ----

def _reimport_constants():
    sys.modules.pop("knarr.core.constants", None)
    return importlib.import_module("knarr.core.constants")


def test_default_cluster_is_devnet(monkeypatch):
    monkeypatch.delenv("KNARR_CLUSTER", raising=False)
    mod = _reimport_constants()
    assert mod.KNARR_CLUSTER == "devnet"
    assert mod.KNARR_MINT == mod.KNARR_MINT_DEVNET


def test_devnet_cluster_selects_devnet_mint(monkeypatch):
    monkeypatch.setenv("KNARR_CLUSTER", "devnet")
    mod = _reimport_constants()
    assert mod.KNARR_CLUSTER == "devnet"
    # Both clusters have no canonical KNR mint yet — empty is correct.
    assert mod.KNARR_MINT == ""
    assert mod.KNARR_MINT_DEVNET == ""


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
