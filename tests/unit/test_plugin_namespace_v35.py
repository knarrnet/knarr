"""V035-001: Plugin loader namespace collision fix.

Two plugins with same-named modules (e.g., both have actions.py) must load
independently without sys.modules collision.
"""

import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from knarr.dht.plugins import PluginHooks, PluginLoader


def _write_file(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


@pytest.fixture
def plugin_dir(tmp_path):
    """Create two plugins that both have an actions.py with ActionExecutor."""
    config_dir = tmp_path / "config"
    plugins = config_dir / "plugins"

    # Plugin A: 01-alpha
    alpha = plugins / "01-alpha"
    _write_file(alpha / "plugin.toml", """\
        name = "alpha"
        version = "1.0"
        handler = "handler:AlphaPlugin"
    """)
    _write_file(alpha / "actions.py", """\
        class ActionExecutor:
            SOURCE = "alpha"
            def __init__(self):
                pass
    """)
    _write_file(alpha / "handler.py", """\
        from knarr.dht.plugins import PluginHooks
        from actions import ActionExecutor

        class AlphaPlugin(PluginHooks):
            def __init__(self, ctx, config=None):
                self._ctx = ctx
                self.executor = ActionExecutor()
                self.source = ActionExecutor.SOURCE
    """)

    # Plugin B: 02-beta (same actions.py module name, different class)
    beta = plugins / "02-beta"
    _write_file(beta / "plugin.toml", """\
        name = "beta"
        version = "1.0"
        handler = "handler:BetaPlugin"
    """)
    _write_file(beta / "actions.py", """\
        class ActionExecutor:
            SOURCE = "beta"
            def __init__(self, db=None):
                self.db = db
    """)
    _write_file(beta / "handler.py", """\
        from knarr.dht.plugins import PluginHooks
        from actions import ActionExecutor

        class BetaPlugin(PluginHooks):
            def __init__(self, ctx, config=None):
                self._ctx = ctx
                self.executor = ActionExecutor(db="test.db")
                self.source = ActionExecutor.SOURCE
    """)

    return config_dir


def _make_loader(config_dir):
    return PluginLoader(
        config_dir=config_dir,
        get_peers_cb=lambda: [],
        send_to_peer_cb=MagicMock(),
        node_id="test_node_id",
    )


class TestNamespaceCollision:
    def test_two_plugins_same_module_name(self, plugin_dir):
        """Both plugins load their OWN actions.py, not the other's."""
        loader = _make_loader(plugin_dir)
        loader.load_plugins()

        assert len(loader.plugins) == 2, f"Expected 2 plugins, got {len(loader.plugins)}"

        alpha = loader.plugins[0]
        beta = loader.plugins[1]

        assert alpha.source == "alpha", f"Alpha got source={alpha.source!r}, expected 'alpha'"
        assert beta.source == "beta", f"Beta got source={beta.source!r}, expected 'beta'"

    def test_beta_constructor_args(self, plugin_dir):
        """Beta's ActionExecutor accepts db= kwarg (alpha's does not)."""
        loader = _make_loader(plugin_dir)
        loader.load_plugins()

        beta = loader.plugins[1]
        assert hasattr(beta.executor, "db")
        assert beta.executor.db == "test.db"

    def test_three_plugins_same_module(self, tmp_path):
        """Three plugins with the same actions.py all get their own copy."""
        config_dir = tmp_path / "config"
        plugins = config_dir / "plugins"

        items = [
            ("alpha", "AlphaPlugin", "aaa"),
            ("bravo", "BravoPlugin", "bbb"),
            ("charlie", "CharliePlugin", "ccc"),
        ]
        for name, cls_name, source in items:
            d = plugins / name
            _write_file(d / "plugin.toml", f"""\
                name = "{name}"
                version = "1.0"
                handler = "handler:{cls_name}"
            """)
            _write_file(d / "actions.py", f"""\
                class ActionExecutor:
                    SOURCE = "{source}"
            """)
            _write_file(d / "handler.py", f"""\
                from knarr.dht.plugins import PluginHooks
                from actions import ActionExecutor

                class {cls_name}(PluginHooks):
                    def __init__(self, ctx, config=None):
                        self._ctx = ctx
                        self.source = ActionExecutor.SOURCE
            """)

        loader = _make_loader(config_dir)
        loader.load_plugins()

        assert len(loader.plugins) == 3
        assert loader.plugins[0].source == "aaa"
        assert loader.plugins[1].source == "bbb"
        assert loader.plugins[2].source == "ccc"


class TestSinglePlugin:
    def test_single_plugin_loads_normally(self, plugin_dir):
        """Single plugin with actions.py loads without issues."""
        import shutil
        shutil.rmtree(plugin_dir / "plugins" / "02-beta")

        loader = _make_loader(plugin_dir)
        loader.load_plugins()

        assert len(loader.plugins) == 1
        assert loader.plugins[0].source == "alpha"
