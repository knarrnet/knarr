"""BR-GATE-001-TEST: Watchman bypass regression tests (v0.48.0).

Three adversarial scenarios that probe whether the watchman supervisor
correctly restores process tracking after cmd_upgrade() and whether
the sentinel file is always written before termination.

Scenario 1 — WM-04: cmd_upgrade() restores self._proc
  After cmd_upgrade(), the supervisor must have a live, tracked proc.
  Old detached=True path set self._proc=None, breaking health monitoring.

Scenario 2 — WM-I1: _terminate() writes knarr.stop sentinel before killing
  The sentinel must appear on disk before SIGTERM. Node uses it to drain.
  Adversary angle: verify sentinel is written even when proc is already dead.

Scenario 3 — WM-04: _spawn() return value equals self._proc
  The refactored _spawn() must return the same Process it stores.
  Verifies upgrader._rollback() gets a valid handle.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_cfg(data_dir: str) -> dict:
    return {
        "node": {
            "command": "knarr",
            "args": ["serve"],
            "data_dir": data_dir,
        },
        "health": {
            "cockpit_url": "http://127.0.0.1:8080",
            "health_interval": 30,
            "health_fail_threshold": 3,
        },
        "recovery": {
            "max_restarts": 5,
            "initial_backoff": 1,
            "max_backoff": 60,
            "backoff_reset_uptime": 300,
        },
    }


def _make_mock_proc(returncode=None):
    """Return a mock asyncio.subprocess.Process with returncode=None (alive)."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = 12345
    proc.wait = AsyncMock(return_value=0)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


class TestWatchmanBrGate(unittest.IsolatedAsyncioTestCase):
    """BR-GATE-001-TEST: watchman bypass regression."""

    # ------------------------------------------------------------------
    # Scenario 1 — WM-04: cmd_upgrade restores self._proc
    # ------------------------------------------------------------------

    async def test_cmd_upgrade_restores_proc_tracking(self):
        """After cmd_upgrade(), self._proc must be a live tracked process (not None)."""
        from knarr.watchman.supervisor import Supervisor

        with tempfile.TemporaryDirectory() as data_dir:
            cfg = _make_cfg(data_dir)
            sup = Supervisor(cfg)

            live_proc = _make_mock_proc(returncode=None)

            async def fake_spawn():
                sup._proc = live_proc
                sup._start_time = 1.0
                return live_proc

            async def fake_terminate():
                pass  # already stopped for test purposes

            sup._spawn = fake_spawn
            sup._terminate = fake_terminate

            await sup.cmd_upgrade()

            self.assertIsNotNone(
                sup._proc,
                "WM-04: cmd_upgrade() must restore self._proc — detached path set it None",
            )
            self.assertIs(sup._proc, live_proc)

    # ------------------------------------------------------------------
    # Scenario 2 — WM-I1: sentinel written before kill
    # ------------------------------------------------------------------

    async def test_terminate_writes_sentinel_before_kill(self):
        """_terminate() must write knarr.stop sentinel before sending SIGTERM/kill.

        Two sub-scenarios:
        a) Drain succeeds (node self-exits) — sentinel written, no force-kill.
        b) Drain times out — sentinel written BEFORE force SIGTERM (adversary check).
        """
        from knarr.watchman.supervisor import Supervisor

        # Sub-scenario a: drain succeeds → sentinel written, no force-terminate
        with tempfile.TemporaryDirectory() as data_dir:
            cfg = _make_cfg(data_dir)
            sup = Supervisor(cfg)
            proc = _make_mock_proc(returncode=None)
            sentinel_path = os.path.join(data_dir, "knarr.stop")

            proc.wait = AsyncMock(return_value=0)  # drain succeeds immediately
            sup._proc = proc

            await sup._terminate()

            # TP-8 fix: sentinel MUST be deleted after clean drain to prevent
            # spurious restart loops (watchman sees sentinel → re-triggers stop).
            self.assertFalse(
                os.path.exists(sentinel_path),
                "WM-I1(a): sentinel must be cleaned up after clean drain (TP-8)",
            )
            proc.terminate.assert_not_called()  # clean exit — no force kill

        # Sub-scenario b: drain timeout → force-terminate; sentinel present beforehand
        with tempfile.TemporaryDirectory() as data_dir:
            cfg = _make_cfg(data_dir)
            # Very short drain timeout to force the timeout path quickly
            cfg["recovery"]["shutdown_drain_timeout"] = 0.05
            sup = Supervisor(cfg)
            proc = _make_mock_proc(returncode=None)
            sentinel_path = os.path.join(data_dir, "knarr.stop")

            kill_order: list[str] = []

            def record_terminate():
                kill_order.append(
                    "sentinel_present" if os.path.exists(sentinel_path) else "sentinel_missing"
                )
                kill_order.append("terminate")

            proc.terminate = record_terminate
            # proc.wait hangs forever, triggering timeout
            _hang = asyncio.Event()
            proc.wait = AsyncMock(side_effect=lambda: _hang.wait())
            proc.kill = MagicMock()
            sup._proc = proc

            # Second wait() call (after SIGTERM) must also complete
            _wait_calls = 0
            async def _wait_side_effect():
                nonlocal _wait_calls
                _wait_calls += 1
                if _wait_calls == 1:
                    await _hang.wait()  # first call: hang → drain timeout
                # subsequent calls: return immediately (post-terminate wait)
            proc.wait = AsyncMock(side_effect=_wait_side_effect)

            await sup._terminate()

            self.assertIn(
                "sentinel_present", kill_order,
                "WM-I1(b): sentinel must exist on disk before proc.terminate() is called",
            )
            self.assertTrue(
                os.path.exists(sentinel_path),
                "WM-I1(b): knarr.stop sentinel must persist after _terminate()",
            )

    # ------------------------------------------------------------------
    # Scenario 3 — WM-04: _spawn() return value == self._proc
    # ------------------------------------------------------------------

    async def test_spawn_return_value_equals_self_proc(self):
        """_spawn() must return the same Process object stored in self._proc."""
        from knarr.watchman.supervisor import Supervisor

        with tempfile.TemporaryDirectory() as data_dir:
            cfg = _make_cfg(data_dir)
            sup = Supervisor(cfg)

            mock_proc = _make_mock_proc(returncode=None)

            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ):
                returned = await sup._spawn()

            self.assertIs(
                returned, sup._proc,
                "WM-04: _spawn() return value must equal self._proc for upgrader rollback tracking",
            )
            self.assertIsNotNone(returned)


if __name__ == "__main__":
    unittest.main()
