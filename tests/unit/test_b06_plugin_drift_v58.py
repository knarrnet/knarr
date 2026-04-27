"""B-06 (v0.58.0): Plugin drift detection.

When a plugin exists as both node-local override and installed package, compare
handler module source bytes. If they differ, emit one WARNING per plugin per
startup with name and short hash prefixes of both sources. Load behavior
unchanged — local still wins.

Scenarios:
- Identical sources → no warning
- Differing sources → WARNING with both hashes
- Only local → no warning
- Only package → no warning
"""
import tempfile
from pathlib import Path

import pytest


def _create_plugin(plugin_dir: Path, name: str, handler_content: str,
                   handler_module: str = "handler"):
    """Create a minimal plugin directory with handler."""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.toml").write_text(
        f'name = "{name}"\nhandler = "{handler_module}:PluginClass"\npriority = 10\n'
    )
    (plugin_dir / f"{handler_module}.py").write_text(handler_content)


class TestDriftDetection:
    """Test _check_plugin_drift directly."""

    def _make_loader(self, config_dir: Path):
        from knarr.dht.plugins import PluginLoader
        return PluginLoader(
            config_dir=config_dir,
            get_peers_cb=lambda: [],
        )

    def test_identical_sources_no_warning(self, caplog):
        """Identical handler source → no drift warning."""
        with tempfile.TemporaryDirectory() as td:
            local_dir = Path(td) / "local_plugins"
            pkg_dir = Path(td) / "pkg_plugins"

            handler = 'from knarr.dht.plugins import PluginHooks\n\nclass PluginClass(PluginHooks): pass\n'

            _create_plugin(local_dir / "myplugin", "myplugin", handler)
            _create_plugin(pkg_dir / "myplugin", "myplugin", handler)

            loader = self._make_loader((local_dir).parent)
            loader._check_plugin_drift(
                plugin_name="myplugin",
                local_path=local_dir / "myplugin",
                package_path=pkg_dir / "myplugin",
                local_config=local_dir / "myplugin" / "plugin.toml",
                package_config=pkg_dir / "myplugin" / "plugin.toml",
            )

            assert "PLUGIN_DRIFT" not in caplog.text

    def test_differing_sources_warning(self, caplog):
        """Differing handler source → drift warning with hashes."""
        with tempfile.TemporaryDirectory() as td:
            local_dir = Path(td) / "local_plugins"
            pkg_dir = Path(td) / "pkg_plugins"

            local_handler = 'from knarr.dht.plugins import PluginHooks\n\nclass PluginClass(PluginHooks):\n    # local version\n    pass\n'
            pkg_handler = 'from knarr.dht.plugins import PluginHooks\n\nclass PluginClass(PluginHooks):\n    # package version\n    pass\n'

            _create_plugin(local_dir / "myplugin", "myplugin", local_handler)
            _create_plugin(pkg_dir / "myplugin", "myplugin", pkg_handler)

            loader = self._make_loader((local_dir).parent)
            loader._check_plugin_drift(
                plugin_name="myplugin",
                local_path=local_dir / "myplugin",
                package_path=pkg_dir / "myplugin",
                local_config=local_dir / "myplugin" / "plugin.toml",
                package_config=pkg_dir / "myplugin" / "plugin.toml",
            )

            assert "PLUGIN_DRIFT" in caplog.text
            assert "name=myplugin" in caplog.text
            assert "local_hash=" in caplog.text
            assert "package_hash=" in caplog.text

    def test_only_local_no_warning(self, caplog):
        """Only local source exists → no warning (package path missing)."""
        with tempfile.TemporaryDirectory() as td:
            local_dir = Path(td) / "local_plugins"
            missing_pkg = Path(td) / "nonexistent"

            handler = 'class PluginClass: pass\n'
            _create_plugin(local_dir / "myplugin", "myplugin", handler)

            loader = self._make_loader((local_dir).parent)
            loader._check_plugin_drift(
                plugin_name="myplugin",
                local_path=local_dir / "myplugin",
                package_path=missing_pkg / "myplugin",
                local_config=local_dir / "myplugin" / "plugin.toml",
                package_config=missing_pkg / "myplugin" / "plugin.toml",
            )

            # Should not raise and should not warn (handler file missing)
            assert "PLUGIN_DRIFT" not in caplog.text

    def test_hash_format(self, caplog):
        """Hash prefixes must be 8 hex chars."""
        with tempfile.TemporaryDirectory() as td:
            local_dir = Path(td) / "local_plugins"
            pkg_dir = Path(td) / "pkg_plugins"

            _create_plugin(local_dir / "myplugin", "myplugin", 'x = 1\n')
            _create_plugin(pkg_dir / "myplugin", "myplugin", 'x = 2\n')

            loader = self._make_loader((local_dir).parent)
            loader._check_plugin_drift(
                plugin_name="myplugin",
                local_path=local_dir / "myplugin",
                package_path=pkg_dir / "myplugin",
                local_config=local_dir / "myplugin" / "plugin.toml",
                package_config=pkg_dir / "myplugin" / "plugin.toml",
            )

            import re
            # Extract hashes from log
            match = re.search(r'local_hash=([0-9a-f]{8})', caplog.text)
            assert match, f"Expected local_hash in log: {caplog.text}"
            match2 = re.search(r'package_hash=([0-9a-f]{8})', caplog.text)
            assert match2, f"Expected package_hash in log: {caplog.text}"
            # Hashes must differ since sources differ
            assert match.group(1) != match2.group(1)
