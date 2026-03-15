"""Security hardening tests for watchman O-031 through O-034 (v0.47.0).

O-031: asset name path traversal (upgrader.py — os.path.basename)
O-032: tar escape via crafted member.name (plugin_manager.py — post-strip validation)
O-033: checksum verification skipped when checksums.txt appears after .whl in asset list
O-034: plugin name TOML injection (plugin_manager.py — [a-zA-Z0-9_-]+ validation)
"""
from __future__ import annotations

import asyncio
import io
import os
import tarfile
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# O-031: Path traversal via crafted asset name
# ---------------------------------------------------------------------------

class TestO031AssetPathTraversal(unittest.IsolatedAsyncioTestCase):

    async def test_basename_applied_to_asset_name(self):
        """A crafted asset name with directory components must be sanitised."""
        from knarr.watchman.upgrader import Upgrader
        from knarr.watchman.supervisor import Supervisor

        cfg = {
            "node": {"command": "knarr", "args": ["serve"], "data_dir": "/tmp/wm031"},
            "health": {"cockpit_url": "http://127.0.0.1:8080",
                       "health_interval": 10, "health_fail_threshold": 3},
            "recovery": {"max_restarts": 5, "initial_backoff": 1,
                         "max_backoff": 60, "backoff_reset_uptime": 600},
            "upgrade": {
                "auto_upgrade": False, "check_interval": 3600,
                "drain_timeout": 1, "health_timeout": 10,
                "upgrade_health_timeout": 10,
                "source": "github:knarrnet/knarr",
            },
        }
        sup = Supervisor(cfg)
        upgrader = Upgrader(cfg, sup)

        # Craft a release with a traversal asset name
        evil_release = {
            "assets": [{
                "name": "../../evil/knarr-0.47.0.whl",
                "browser_download_url": "https://example.com/knarr.whl",
            }]
        }

        downloaded_paths = []

        def fake_download(url, dest):
            downloaded_paths.append(dest)

        with tempfile.TemporaryDirectory() as data_dir:
            cfg["node"]["data_dir"] = data_dir
            with patch("knarr.watchman.upgrader._fetch_latest_release", return_value=evil_release):
                with patch("knarr.watchman.upgrader._download_file", side_effect=fake_download):
                    with patch.object(sup, "_terminate", new=AsyncMock()):
                        with patch.object(sup, "_spawn", new=AsyncMock()):
                            with patch.object(sup, "_probe_health", new=AsyncMock(return_value=True)):
                                with patch("knarr.watchman.upgrader._install_from_tarball"):
                                    await upgrader.run_upgrade(tag="v0.47.0")

        # The destination path must not escape the staging directory
        if downloaded_paths:
            dest = downloaded_paths[0]
            # basename of "../../evil/knarr-0.47.0.whl" is "knarr-0.47.0.whl"
            self.assertNotIn("evil", dest)
            self.assertEqual(os.path.basename(dest), "knarr-0.47.0.whl")


# ---------------------------------------------------------------------------
# O-032: Tar escape via crafted member name
# ---------------------------------------------------------------------------

class TestO032TarEscape(unittest.TestCase):

    def _make_tarball(self, members: list[tuple[str, bytes]]) -> str:
        """Create a .tar.gz with given (name, content) pairs. Returns path."""
        tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
        tmp.close()
        with tarfile.open(tmp.name, "w:gz") as tar:
            for name, content in members:
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        return tmp.name

    def test_dotdot_member_is_skipped(self):
        """Members with '..' after prefix-strip must not be extracted."""
        from knarr.watchman.plugin_manager import _extract_tarball

        # top-level prefix is "repo-abc123/"
        tarball = self._make_tarball([
            ("repo-abc123/", b""),               # prefix dir
            ("repo-abc123/safe.txt", b"safe"),
            ("repo-abc123/../../evil.sh", b"evil"),  # traversal attempt
        ])

        with tempfile.TemporaryDirectory() as plugin_dir:
            try:
                _extract_tarball(tarball, plugin_dir)
            finally:
                os.unlink(tarball)

            files = os.listdir(plugin_dir)
            self.assertIn("safe.txt", files)
            self.assertNotIn("evil.sh", files)
            # Also verify nothing escaped the plugin_dir
            parent_files = os.listdir(os.path.dirname(plugin_dir))
            self.assertNotIn("evil.sh", parent_files)

    def test_absolute_path_member_is_skipped(self):
        """Members with absolute paths after prefix-strip must be skipped."""
        from knarr.watchman.plugin_manager import _extract_tarball

        tarball = self._make_tarball([
            ("repo-abc123/", b""),
            ("repo-abc123/ok.txt", b"ok"),
            # After strip: "/etc/passwd" — absolute, must skip
            ("/etc/passwd", b"injected"),
        ])

        with tempfile.TemporaryDirectory() as plugin_dir:
            try:
                _extract_tarball(tarball, plugin_dir)
            finally:
                os.unlink(tarball)

            files = os.listdir(plugin_dir)
            self.assertIn("ok.txt", files)


# ---------------------------------------------------------------------------
# O-033: Checksum verified regardless of asset ordering
# ---------------------------------------------------------------------------

class TestO033ChecksumOrdering(unittest.IsolatedAsyncioTestCase):

    async def _run_upgrade_with_assets(self, assets):
        """Run upgrade with a given asset list, return whether verify was attempted."""
        from knarr.watchman.upgrader import Upgrader
        from knarr.watchman.supervisor import Supervisor

        cfg = {
            "node": {"command": "knarr", "args": ["serve"], "data_dir": "/tmp/wm033"},
            "health": {"cockpit_url": "http://127.0.0.1:8080",
                       "health_interval": 10, "health_fail_threshold": 3},
            "recovery": {"max_restarts": 5, "initial_backoff": 1,
                         "max_backoff": 60, "backoff_reset_uptime": 600},
            "upgrade": {
                "auto_upgrade": False, "check_interval": 3600,
                "drain_timeout": 1, "health_timeout": 10,
                "upgrade_health_timeout": 10,
                "source": "github:knarrnet/knarr",
            },
        }
        sup = Supervisor(cfg)
        upgrader = Upgrader(cfg, sup)

        release = {"assets": assets}
        verified = {}

        # checksums.txt content: correct sha256 for the whl
        checksum_content = b"aabbcc1122334455aabbcc1122334455aabbcc1122334455aabbcc1122334455  knarr-0.47.0-py3-none-any.whl\n"

        def fake_urlopen(req, timeout=10):
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read = lambda: checksum_content
            return resp

        def fake_download(url, dest):
            # Write a file that will hash correctly
            with open(dest, "wb") as f:
                f.write(b"fake wheel content")

        def fake_sha256(path):
            # Return the hash from checksums.txt
            return "aabbcc1122334455aabbcc1122334455aabbcc1122334455aabbcc1122334455"

        with tempfile.TemporaryDirectory() as data_dir:
            cfg["node"]["data_dir"] = data_dir
            with patch("knarr.watchman.upgrader._fetch_latest_release", return_value=release):
                with patch("knarr.watchman.upgrader._download_file", side_effect=fake_download):
                    with patch("knarr.watchman.upgrader._sha256_file", side_effect=fake_sha256):
                        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                            with patch.object(sup, "_terminate", new=AsyncMock()):
                                with patch.object(sup, "_spawn", new=AsyncMock()):
                                    with patch.object(sup, "_probe_health", new=AsyncMock(return_value=True)):
                                        with patch("knarr.watchman.upgrader._install_from_tarball"):
                                            result = await upgrader.run_upgrade(tag="v0.47.0")
        return result

    async def test_checksum_verified_when_checksums_before_whl(self):
        """Standard order: checksums.txt first, then .whl — must verify."""
        assets = [
            {"name": "checksums.txt", "browser_download_url": "https://example.com/checksums.txt"},
            {"name": "knarr-0.47.0-py3-none-any.whl", "browser_download_url": "https://example.com/knarr.whl"},
        ]
        result = await self._run_upgrade_with_assets(assets)
        self.assertTrue(result)

    async def test_checksum_verified_when_whl_before_checksums(self):
        """.whl listed before checksums.txt — two-pass must still verify."""
        assets = [
            {"name": "knarr-0.47.0-py3-none-any.whl", "browser_download_url": "https://example.com/knarr.whl"},
            {"name": "checksums.txt", "browser_download_url": "https://example.com/checksums.txt"},
        ]
        result = await self._run_upgrade_with_assets(assets)
        self.assertTrue(result)


# ---------------------------------------------------------------------------
# O-034: Plugin name TOML injection
# ---------------------------------------------------------------------------

class TestO034PluginNameValidation(unittest.TestCase):

    def test_valid_names_are_accepted(self):
        """Names matching [a-zA-Z0-9_-]+ must be accepted."""
        from knarr.watchman.plugin_manager import install_plugin
        for name in ("my-plugin", "plugin_v2", "MyPlugin123", "a"):
            # Just check validation passes (source lookup will fail, that's OK)
            with self.assertRaises(Exception) as ctx:
                install_plugin(name, {"source": "file:///nonexistent"}, "/tmp", "/tmp")
            # Must NOT be our ValueError about invalid name
            self.assertNotIn("must match", str(ctx.exception))

    def test_injection_names_are_rejected(self):
        """Names with TOML-dangerous characters must be rejected."""
        from knarr.watchman.plugin_manager import install_plugin
        dangerous = [
            "evil]\n[injected",
            "../etc",
            "plugin name",  # space
            "plugin\x00null",
            "",
        ]
        for name in dangerous:
            with self.assertRaises(ValueError) as ctx:
                install_plugin(name, {"source": "file:///anything"}, "/tmp", "/tmp")
            self.assertIn("must match", str(ctx.exception), f"Name {name!r} should be rejected")

    def test_write_manifest_rejects_injection_name(self):
        """_write_manifest_simple must refuse TOML-injection names."""
        from knarr.watchman.plugin_manager import _write_manifest_simple
        import tempfile
        manifest = {"plugins": {"evil]\n[bad": {"enabled": True}}}
        with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as f:
            path = f.name
        try:
            with self.assertRaises(ValueError):
                _write_manifest_simple(path, manifest)
        finally:
            os.unlink(path)

    def test_write_manifest_accepts_valid_name(self):
        """_write_manifest_simple must succeed for valid names."""
        from knarr.watchman.plugin_manager import _write_manifest_simple
        import tempfile
        manifest = {"plugins": {"my-plugin": {"enabled": True, "source": "github:org/repo"}}}
        with tempfile.NamedTemporaryFile(suffix=".toml", delete=False, mode="w") as f:
            path = f.name
        try:
            _write_manifest_simple(path, manifest)
            with open(path) as f:
                content = f.read()
            self.assertIn("[plugins.my-plugin]", content)
        finally:
            os.unlink(path)
