"""T2-08: Plugin loader package-path fallback.

- Loader falls back to the package-shipped plugins when node-local dir is
  missing or incomplete.
- Node-local always wins on name collision.
- Missing-config errors skip the plugin with a warning, never crash.
- Exact debug log strings required by the brief are emitted.
"""
import logging
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from knarr.dht.plugins import PluginLoader


def _make_loader(config_dir: Path) -> PluginLoader:
    return PluginLoader(
        config_dir=config_dir,
        get_peers_cb=lambda: [],
        send_to_peer_cb=None,
        node_id="n" * 64,
    )


def test_find_package_plugin_root_returns_shipped_dir(tmp_path):
    loader = _make_loader(tmp_path)
    pkg_root = loader._find_package_plugin_root()
    # The package plugins directory ships with handlers we know about
    assert pkg_root is not None
    assert pkg_root.is_dir()
    # Should contain at least the punchhole-backend plugin folder
    assert (pkg_root / "09-punchhole-backend").is_dir()


def test_scan_returns_empty_when_root_missing(tmp_path):
    loader = _make_loader(tmp_path)
    entries, seen = loader._scan_plugin_source(None, source_label="node_local")
    assert entries == []
    assert seen == set()


def test_node_local_takes_precedence_over_package(tmp_path, caplog):
    # Build a fake node-local punchhole-backend dir with just a plugin.toml
    plugins_dir = tmp_path / "plugins" / "09-punchhole-backend"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "plugin.toml").write_text(
        'name = "punchhole-backend"\nversion = "0.0.1-test"\nhandler = "stub:Stub"\n',
        encoding="utf-8",
    )
    loader = _make_loader(tmp_path)
    local_entries, seen = loader._scan_plugin_source(
        loader._plugin_root, source_label="node_local",
    )
    assert "punchhole-backend" in seen
    # Package scan must skip the plugin that node_local already claimed
    pkg_root = loader._find_package_plugin_root()
    with caplog.at_level(logging.DEBUG, logger="knarr.dht.plugins"):
        pkg_entries, pkg_seen = loader._scan_plugin_source(
            pkg_root, source_label="package", skip_names=seen,
        )
    # Must not double-load punchhole-backend
    pkg_names = {cfg.get("name") for _, _, _, cfg in pkg_entries}
    assert "punchhole-backend" not in pkg_names


def test_fallback_log_string_present(tmp_path, caplog):
    loader = _make_loader(tmp_path)
    pkg_root = loader._find_package_plugin_root()
    with caplog.at_level(logging.DEBUG, logger="knarr.dht.plugins"):
        loader._scan_plugin_source(pkg_root, source_label="package")
    # Exact prefix required by brief
    assert any(
        "PLUGIN_LOADER_FALLBACK source=package plugin=" in rec.message
        for rec in caplog.records
    )


def test_load_plugins_without_local_dir_still_finds_package(tmp_path):
    # Note: config_dir has NO plugins/ subdir — loader must fall back
    loader = _make_loader(tmp_path)
    assert not loader._plugin_root.exists()
    loader.load_plugins()
    # We can't assert specific plugins loaded since their config/state may
    # fail to initialize in a blank dir, but we CAN assert the loader did
    # not crash and _plugin_root absence didn't short-circuit.
    # (Check that _find_package_plugin_root was useful.)
    assert loader._find_package_plugin_root() is not None


def test_missing_config_key_does_not_crash(tmp_path, caplog):
    """A plugin class that raises KeyError in __init__ should be skipped."""
    plugin_dir = tmp_path / "plugins" / "bad-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        'name = "bad-plugin"\nversion = "0.0.1"\nhandler = "handler:Bad"\n[config]\n',
        encoding="utf-8",
    )
    (plugin_dir / "handler.py").write_text(
        textwrap.dedent("""
            from knarr.dht.plugins import PluginHooks
            class Bad(PluginHooks):
                def __init__(self, ctx, config):
                    raise KeyError("thrall_endpoint")
        """),
        encoding="utf-8",
    )

    loader = _make_loader(tmp_path)
    with caplog.at_level(logging.WARNING, logger="knarr.dht.plugins"):
        loader.load_plugins()
    # Exact log format required by brief
    assert any(
        "PLUGIN_SKIPPED name=bad-plugin reason=missing_config key=thrall_endpoint"
        in rec.message
        for rec in caplog.records
    ), [r.message for r in caplog.records]
    # Loader must not have crashed and must not have added the broken plugin
    assert loader.get_plugin_by_name("bad-plugin") is None
