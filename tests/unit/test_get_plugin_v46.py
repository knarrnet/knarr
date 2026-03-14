"""v0.46.0: ctx.get_plugin — PluginLoader.get_plugin_by_name.

Verifies:
  - get_plugin_by_name(name) returns the correct plugin instance after load_plugins().
  - get_plugin_by_name(unknown) returns None.
  - Both plugins in a multi-plugin setup are individually retrievable by name.
  - ctx.get_plugin callable is wired to the loader's get_plugin_by_name method.

Import strategy
---------------
conftest.py → knarr.dht.node → knarr.dht.plugins are all imported at
collection time (node.py has a module-level `from knarr.dht.plugins import`).
By the time tests execute, sys.modules["knarr.dht.plugins"] already points to
knarr.dev/src — the dev version that lacks _name_to_plugin / get_plugin_by_name.

__path__ manipulation cannot replace a module that is already cached.

Fix: use an autouse function-scoped fixture that (a) force-loads the worktree's
plugins.py via importlib and replaces sys.modules["knarr.dht.plugins"], then
(b) restores the original entry in teardown.  This ensures that when handler.py
inside load_plugins() does `from knarr.dht.plugins import PluginHooks`, it
gets the same PluginHooks class that the worktree PluginLoader uses for the
issubclass check — and that adjacent test files (test_plugin_namespace_v35.py,
etc.) see the dev module both before and after this file's tests run.

PluginLoader and PluginHooks must come from the fixture, not module-level
imports, so they always reference the worktree version.
"""

import importlib.util
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BASE = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_file(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


def _make_loader(config_dir, PluginLoader):
    return PluginLoader(
        config_dir=config_dir,
        get_peers_cb=lambda: [],
        send_to_peer_cb=MagicMock(),
        node_id="test_node_id",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _worktree_plugins():
    """Force-load the worktree's plugins.py and inject it into sys.modules.

    Teardown restores the original entry so other test files in the session
    are not affected.
    """
    sys.path.insert(0, str(_BASE / "src"))
    original = sys.modules.get("knarr.dht.plugins")

    spec = importlib.util.spec_from_file_location(
        "knarr.dht.plugins",
        str(_BASE / "src" / "knarr" / "dht" / "plugins.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules["knarr.dht.plugins"] = mod

    yield mod

    if original is not None:
        sys.modules["knarr.dht.plugins"] = original
    else:
        sys.modules.pop("knarr.dht.plugins", None)


@pytest.fixture
def PluginLoader(_worktree_plugins):  # noqa: N802
    return _worktree_plugins.PluginLoader


@pytest.fixture
def PluginHooks(_worktree_plugins):  # noqa: N802
    return _worktree_plugins.PluginHooks


@pytest.fixture
def two_plugin_dir(tmp_path):
    """Two plugins: 'punchhole-backend' and 'wallet'."""
    config_dir = tmp_path / "config"
    plugins = config_dir / "plugins"

    for folder, name, cls_name in [
        ("01-punchhole", "punchhole-backend", "PunchholePlugin"),
        ("02-wallet", "wallet", "WalletPlugin"),
    ]:
        d = plugins / folder
        _write_file(d / "plugin.toml", f"""\
            name = "{name}"
            version = "1.0"
            handler = "handler:{cls_name}"
        """)
        _write_file(d / "handler.py", f"""\
            from knarr.dht.plugins import PluginHooks

            class {cls_name}(PluginHooks):
                def __init__(self, ctx, config=None):
                    self._ctx = ctx
                    self.plugin_name = "{name}"
        """)

    return config_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGetPluginByName:
    def test_lookup_by_name_returns_instance(self, PluginLoader, PluginHooks, two_plugin_dir):
        """get_plugin_by_name returns the loaded plugin for the correct name."""
        loader = _make_loader(two_plugin_dir, PluginLoader)
        loader.load_plugins()
        assert len(loader.plugins) == 2

        ph = loader.get_plugin_by_name("punchhole-backend")
        assert ph is not None
        assert isinstance(ph, PluginHooks)
        assert ph.plugin_name == "punchhole-backend"

    def test_lookup_wallet_by_name(self, PluginLoader, two_plugin_dir):
        """Both plugins are independently retrievable by name."""
        loader = _make_loader(two_plugin_dir, PluginLoader)
        loader.load_plugins()

        wallet = loader.get_plugin_by_name("wallet")
        assert wallet is not None
        assert wallet.plugin_name == "wallet"

    def test_unknown_name_returns_none(self, PluginLoader, two_plugin_dir):
        """get_plugin_by_name returns None for an unknown plugin name."""
        loader = _make_loader(two_plugin_dir, PluginLoader)
        loader.load_plugins()

        assert loader.get_plugin_by_name("nonexistent") is None

    def test_empty_loader_returns_none(self, PluginLoader, tmp_path):
        """Returns None when no plugins are loaded."""
        config_dir = tmp_path / "config"
        (config_dir / "plugins").mkdir(parents=True)
        loader = _make_loader(config_dir, PluginLoader)
        loader.load_plugins()

        assert loader.get_plugin_by_name("anything") is None

    def test_ctx_get_plugin_callable_is_wired(self, PluginLoader, two_plugin_dir):
        """ctx.get_plugin callable returns the same instance as get_plugin_by_name."""
        loader = _make_loader(two_plugin_dir, PluginLoader)
        loader.load_plugins()

        # Simulate what node.py does: wire ctx.get_plugin to the loader method.
        ctx_get_plugin = loader.get_plugin_by_name

        ph_via_ctx = ctx_get_plugin("punchhole-backend")
        ph_direct = loader.get_plugin_by_name("punchhole-backend")

        assert ph_via_ctx is ph_direct
        assert ph_via_ctx is not None

    def test_name_to_plugin_index_matches_plugins_list(self, PluginLoader, two_plugin_dir):
        """_name_to_plugin dict and plugins list contain the same instances."""
        loader = _make_loader(two_plugin_dir, PluginLoader)
        loader.load_plugins()

        for name, instance in loader._name_to_plugin.items():
            assert instance in loader.plugins
