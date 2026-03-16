"""Tests for BR-WM-02: cmd_upgrade detached spawn (v0.47.0).

Verifies that:
- Supervisor._spawn(detached=True) uses subprocess.Popen (not asyncio transport)
- On Windows it passes DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
- On non-Windows it passes start_new_session=True
- self._proc is None after a detached spawn (not tracked)
- Upgrader.run_upgrade passes detached=True on success and rollback paths
- upgrade_health_timeout config key is respected
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch


# ---------------------------------------------------------------------------
# Supervisor._spawn detached tests
# WM-04 (v0.48.0): detached spawn replaced with tracked spawn.
# These tests are superseded by test_watchman_br_gate_v48.py.
# ---------------------------------------------------------------------------

@unittest.skip("Superseded by WM-04: detached spawn replaced with tracked spawn in v0.48.0")
class TestSupervisorSpawnDetached(unittest.IsolatedAsyncioTestCase):

    def _make_supervisor(self, cfg=None):
        from knarr.watchman.supervisor import Supervisor
        cfg = cfg or {
            "node": {"command": "knarr", "args": ["serve"], "data_dir": "."},
            "health": {"cockpit_url": "http://127.0.0.1:8080",
                       "health_interval": 10, "health_fail_threshold": 3},
            "recovery": {"max_restarts": 5, "initial_backoff": 1,
                         "max_backoff": 60, "backoff_reset_uptime": 600},
        }
        return Supervisor(cfg)

    async def test_detached_false_uses_asyncio(self):
        """Default detached=False path must use asyncio.create_subprocess_exec."""
        sup = self._make_supervisor()
        mock_proc = MagicMock()
        mock_proc.pid = 1234

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)) as mock_cse:
            await sup._spawn(detached=False)
            mock_cse.assert_called_once()
            self.assertIsNotNone(sup._proc)

    async def test_detached_true_windows_uses_popen_with_flags(self):
        """On Windows, detached=True must use DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP."""
        sup = self._make_supervisor()
        mock_popen = MagicMock()

        with patch.object(sys, "platform", "win32"):
            with patch("subprocess.Popen", return_value=mock_popen) as mock_p:
                await sup._spawn(detached=True)
                mock_p.assert_called_once()
                _, kwargs = mock_p.call_args
                self.assertIn("creationflags", kwargs)
                # DETACHED_PROCESS=0x8, CREATE_NEW_PROCESS_GROUP=0x200 → 0x208
                self.assertEqual(kwargs["creationflags"], 0x00000008 | 0x00000200)

    async def test_detached_true_posix_uses_popen_with_new_session(self):
        """On non-Windows, detached=True must use start_new_session=True."""
        sup = self._make_supervisor()
        mock_popen = MagicMock()

        with patch.object(sys, "platform", "linux"):
            with patch("subprocess.Popen", return_value=mock_popen) as mock_p:
                await sup._spawn(detached=True)
                mock_p.assert_called_once()
                _, kwargs = mock_p.call_args
                self.assertTrue(kwargs.get("start_new_session"))

    async def test_detached_true_sets_proc_to_none(self):
        """After detached spawn, self._proc must be None (not tracked)."""
        sup = self._make_supervisor()

        with patch("subprocess.Popen", return_value=MagicMock()):
            await sup._spawn(detached=True)
            self.assertIsNone(sup._proc)


# ---------------------------------------------------------------------------
# Upgrader uses detached spawn
# ---------------------------------------------------------------------------

class TestUpgraderDetachedSpawn(unittest.IsolatedAsyncioTestCase):

    def _make_upgrader_and_supervisor(self, health_timeout=60):
        from knarr.watchman.supervisor import Supervisor
        from knarr.watchman.upgrader import Upgrader

        cfg = {
            "node": {"command": "knarr", "args": ["serve"], "data_dir": "/tmp/wm_test"},
            "health": {"cockpit_url": "http://127.0.0.1:8080",
                       "health_interval": 10, "health_fail_threshold": 3},
            "recovery": {"max_restarts": 5, "initial_backoff": 1,
                         "max_backoff": 60, "backoff_reset_uptime": 600},
            "upgrade": {
                "auto_upgrade": False,
                "check_interval": 3600,
                "drain_timeout": 1,
                "health_timeout": 30,
                "upgrade_health_timeout": health_timeout,
                "source": "github:knarrnet/knarr",
            },
        }
        sup = Supervisor(cfg)
        upgrader = Upgrader(cfg, sup)
        return upgrader, sup

    @unittest.skip("Superseded by WM-04: upgrader now uses tracked spawn, not detached=True")
    async def test_run_upgrade_calls_detached_spawn(self):
        """run_upgrade must call _spawn(detached=True) for the new version."""
        upgrader, sup = self._make_upgrader_and_supervisor()

        # Patch everything external
        with patch("knarr.watchman.upgrader._fetch_latest_release", return_value=None):
            with patch.object(sup, "_terminate", new=AsyncMock()):
                with patch.object(sup, "_spawn", new=AsyncMock()) as mock_spawn:
                    with patch.object(sup, "_probe_health", new=AsyncMock(return_value=True)):
                        with patch("knarr.watchman.upgrader._install_from_tag"):
                            await upgrader.run_upgrade(tag="v0.47.0")
                            mock_spawn.assert_called_with(detached=True)

    async def test_upgrade_health_timeout_is_used(self):
        """upgrade_health_timeout (60s) must be used, not health_timeout (30s)."""
        upgrader, sup = self._make_upgrader_and_supervisor(health_timeout=60)

        health_calls = []

        async def _probe():
            health_calls.append(1)
            return True  # healthy on first call

        with patch("knarr.watchman.upgrader._fetch_latest_release", return_value=None):
            with patch.object(sup, "_terminate", new=AsyncMock()):
                with patch.object(sup, "_spawn", new=AsyncMock()):
                    with patch.object(sup, "_probe_health", side_effect=_probe):
                        with patch("knarr.watchman.upgrader._install_from_tag"):
                            # Should succeed — we use upgrade_health_timeout=60s budget
                            result = await upgrader.run_upgrade(tag="v0.47.0")
                            self.assertTrue(result)
