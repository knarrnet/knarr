import logging
import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from knarr.dht.plugins import PluginLoader


def _write_file(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _write_plugin(plugin_dir: Path, name: str):
    class_name = "".join(part.capitalize() for part in name.split("-")) + "Plugin"
    _write_file(
        plugin_dir / "plugin.toml",
        f"""
        name = "{name}"
        version = "1.0"
        handler = "handler:{class_name}"
        """,
    )
    _write_file(
        plugin_dir / "handler.py",
        f"""
        from knarr.dht.plugins import PluginHooks

        class {class_name}(PluginHooks):
            def __init__(self, ctx, config=None):
                self._ctx = ctx
                self.name = "{name}"
        """,
    )


def _symlink_dir(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(src, dst, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")


def _loader(config_dir: Path):
    return PluginLoader(
        config_dir=config_dir,
        get_peers_cb=lambda: [],
        send_to_peer_cb=MagicMock(),
        node_id="test_node",
    )


def test_plugin_root_inside_prefix_loads(tmp_path):
    config_dir = tmp_path / "config"
    _write_plugin(config_dir / "plugins" / "01-inside", "inside")

    loader = _loader(config_dir)
    loader.load_plugins()

    plugin = loader.get_plugin_by_name("inside")
    assert plugin is not None
    assert plugin.name == "inside"


def test_plugin_root_symlink_target_outside_prefix_rejected(tmp_path, caplog):
    config_dir = tmp_path / "config"
    outside_plugins = tmp_path / "outside-plugins"
    _write_plugin(outside_plugins / "01-escape", "escape")
    _symlink_dir(outside_plugins, config_dir / "plugins")

    loader = _loader(config_dir)
    with caplog.at_level(logging.WARNING, logger="knarr.dht.plugins"):
        loader.load_plugins()

    assert loader.get_plugin_by_name("escape") is None
    assert "PLUGIN_ROOT_REJECTED" in caplog.text
    assert str(config_dir / "plugins" / "01-escape") in caplog.text
    assert str((outside_plugins / "01-escape").resolve()) in caplog.text


def test_plugin_name_inside_but_resolved_root_outside_rejected(tmp_path, caplog):
    config_dir = tmp_path / "config"
    plugins_dir = config_dir / "plugins"
    outside = tmp_path / "outside-plugin" / "01-escape"
    _write_plugin(outside, "escape")
    _symlink_dir(outside, plugins_dir / "01-escape")

    loader = _loader(config_dir)
    with caplog.at_level(logging.WARNING, logger="knarr.dht.plugins"):
        loader.load_plugins()

    assert loader.get_plugin_by_name("escape") is None
    assert "PLUGIN_ROOT_REJECTED" in caplog.text
    assert str(plugins_dir / "01-escape") in caplog.text
    assert str(outside.resolve()) in caplog.text
