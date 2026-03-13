"""A7 contract test: BCW must fail loudly when no valid chains are configured.

E-028: handler.py:287 silently continues when configured chain_id doesn't match
any loaded Solana module. Plugin initialises with _enabled=True but _solana_modules
is empty — all BCW operations silently no-op with no log warning. An operator
with a misconfigured chain_id will think BCW is working but nothing happens.

FIX LOCATION: plugins/10-bcw/handler.py — __init__() after the chains loop:
    if self._enabled and not self._solana_modules:
        self._enabled = False
        self._log_warning("BCW disabled: no valid chains configured (check chain_id in plugin.toml)")

CONTRACT:
- BCW initialized with an invalid/unknown chain_id must have _enabled=False after init.
- BCW initialized with valid chain_id must have _enabled=True (no regression).
- BCW initialized with no chains config at all must have _enabled=False.
- A warning must be logged when BCW is disabled due to empty solana_modules.
"""
import pytest
from unittest.mock import MagicMock, patch


def _make_ctx():
    ctx = MagicMock()
    ctx.node_id = "aa" * 32
    ctx.storage_path = None
    ctx.state_dir = None
    ctx.plugin_dir = MagicMock()
    ctx.plugin_dir.__truediv__ = lambda self, other: MagicMock()
    ctx.subscribe_events = None
    ctx.vault_get = MagicMock(return_value=b"\x01" * 32)  # valid seed
    return ctx


def _import_bcw():
    import sys, pathlib
    plugin_path = str(pathlib.Path(__file__).parents[4] / "knarr.clean" / "src" / "knarr" / "plugins" / "10-bcw")
    if plugin_path not in sys.path:
        sys.path.insert(0, plugin_path)
    from handler import BCWHandler
    return BCWHandler


def test_invalid_chain_id_disables_bcw():
    """BCW with unknown chain_id must be disabled after init."""
    BCWHandler = _import_bcw()
    ctx = _make_ctx()
    config = {
        "enabled": True,
        "chains": [{"chain_id": "not-a-real-chain", "rpc_url": "https://fake.rpc"}],
    }

    with patch("handler.WatchStore.__init__", lambda self, *a, **kw: None), \
         patch("handler.WatchStore.get_all_watches", return_value=[]):
        handler = BCWHandler.__new__(BCWHandler)
        BCWHandler.__init__(handler, ctx, config)

    assert handler._enabled is False, (
        "_enabled must be False when all chain_ids are invalid. "
        "Fix: add 'if self._enabled and not self._solana_modules: self._enabled = False'."
    )


def test_invalid_chain_id_logs_warning():
    """BCW disabled due to empty solana_modules must emit a warning."""
    BCWHandler = _import_bcw()
    ctx = _make_ctx()
    config = {
        "enabled": True,
        "chains": [{"chain_id": "not-a-real-chain"}],
    }
    warnings = []

    with patch("handler.WatchStore.__init__", lambda self, *a, **kw: None), \
         patch("handler.WatchStore.get_all_watches", return_value=[]):
        handler = BCWHandler.__new__(BCWHandler)
        with patch.object(BCWHandler, "_log_warning",
                          side_effect=lambda msg, *a, **kw: warnings.append(msg % a if a else msg)):
            BCWHandler.__init__(handler, ctx, config)

    assert any("chain" in w.lower() or "disabled" in w.lower() for w in warnings), (
        "BCW must log a warning when disabled due to no valid chains. "
        f"Warnings logged: {warnings}"
    )


def test_no_chains_config_disables_bcw():
    """BCW with no chains config at all must be disabled."""
    BCWHandler = _import_bcw()
    ctx = _make_ctx()
    config = {"enabled": True, "chains": []}

    with patch("handler.WatchStore.__init__", lambda self, *a, **kw: None), \
         patch("handler.WatchStore.get_all_watches", return_value=[]):
        handler = BCWHandler.__new__(BCWHandler)
        BCWHandler.__init__(handler, ctx, config)

    assert handler._enabled is False, (
        "_enabled must be False when chains list is empty. "
        "An enabled BCW with no chains configured is silently broken."
    )


def test_valid_chain_id_keeps_bcw_enabled():
    """BCW with a supported chain_id must remain enabled (no regression)."""
    BCWHandler = _import_bcw()
    ctx = _make_ctx()
    config = {
        "enabled": True,
        "chains": [{"chain_id": "solana-devnet"}],
    }

    with patch("handler.WatchStore.__init__", lambda self, *a, **kw: None), \
         patch("handler.WatchStore.get_all_watches", return_value=[]), \
         patch("handler.SolanaWatcher.__init__", lambda self, *a, **kw: None):
        handler = BCWHandler.__new__(BCWHandler)
        BCWHandler.__init__(handler, ctx, config)

    assert handler._enabled is True, (
        "BCW must remain enabled when a valid chain_id is configured. "
        "Fix must not disable BCW when solana_modules is non-empty."
    )
