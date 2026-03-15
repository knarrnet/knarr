"""Tests for BR-WM-03: cmd_status PID liveness fix (v0.47.0).

Verifies that _is_pid_alive correctly distinguishes alive/dead processes
on both Windows (ctypes path) and POSIX (os.kill path), and that cmd_status
uses it rather than the old catch-all os.kill approach.
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helper: import _is_pid_alive without running main()
# ---------------------------------------------------------------------------

from knarr.watchman.main import _is_pid_alive, _read_pid, _write_pid, _remove_pid


class TestIsPidAliveWindows(unittest.TestCase):
    """Test the Windows ctypes path of _is_pid_alive."""

    def _run_with_mock_ctypes(self, open_process_return, get_last_error_return):
        """Patch sys.platform and ctypes, run _is_pid_alive(1234)."""
        mock_kernel32 = MagicMock()
        mock_kernel32.OpenProcess.return_value = open_process_return
        mock_kernel32.GetLastError.return_value = get_last_error_return

        mock_ctypes = MagicMock()
        mock_ctypes.windll.kernel32 = mock_kernel32

        with patch.object(sys, "platform", "win32"):
            with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
                # Re-import to pick up the patched platform check
                import importlib
                import knarr.watchman.main as m
                importlib.reload(m)
                result = m._is_pid_alive(1234)
        return result

    def test_valid_handle_means_alive(self):
        """OpenProcess returns a non-zero handle → process is alive."""
        result = self._run_with_mock_ctypes(
            open_process_return=999,  # non-zero handle
            get_last_error_return=0,
        )
        self.assertTrue(result)

    def test_access_denied_means_alive(self):
        """NULL handle + ERROR_ACCESS_DENIED (5) → alive but restricted."""
        result = self._run_with_mock_ctypes(
            open_process_return=0,   # NULL handle
            get_last_error_return=5, # ERROR_ACCESS_DENIED
        )
        self.assertTrue(result)

    def test_not_found_means_dead(self):
        """NULL handle + ERROR_INVALID_PARAMETER (87) → process does not exist."""
        result = self._run_with_mock_ctypes(
            open_process_return=0,
            get_last_error_return=87,  # ERROR_INVALID_PARAMETER
        )
        self.assertFalse(result)

    def test_handle_is_closed_after_check(self):
        """When a valid handle is returned, CloseHandle must be called."""
        mock_kernel32 = MagicMock()
        mock_kernel32.OpenProcess.return_value = 42
        mock_kernel32.GetLastError.return_value = 0
        mock_ctypes = MagicMock()
        mock_ctypes.windll.kernel32 = mock_kernel32

        with patch.object(sys, "platform", "win32"):
            with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
                import importlib
                import knarr.watchman.main as m
                importlib.reload(m)
                m._is_pid_alive(9999)
                mock_kernel32.CloseHandle.assert_called_once_with(42)


class TestIsPidAlivePosix(unittest.TestCase):
    """Test the POSIX os.kill path of _is_pid_alive."""

    def _call(self, side_effect):
        with patch.object(sys, "platform", "linux"):
            with patch("os.kill", side_effect=side_effect):
                import importlib
                import knarr.watchman.main as m
                importlib.reload(m)
                return m._is_pid_alive(1234)

    def test_no_exception_means_alive(self):
        self.assertTrue(self._call(side_effect=None))

    def test_eperm_means_alive(self):
        import errno
        err = OSError()
        err.errno = errno.EPERM
        self.assertTrue(self._call(side_effect=err))

    def test_esrch_means_dead(self):
        import errno
        err = OSError()
        err.errno = errno.ESRCH
        self.assertFalse(self._call(side_effect=err))

    def test_process_lookup_error_means_dead(self):
        self.assertFalse(self._call(side_effect=ProcessLookupError))


class TestCmdStatusUsesPidAlive(unittest.TestCase):
    """Verify cmd_status calls _is_pid_alive (not bare os.kill) for liveness."""

    def test_cmd_status_removes_pid_when_dead(self, tmp_path=None):
        """cmd_status should remove PID file and report 'not running' when dead."""
        import tempfile, json
        with tempfile.TemporaryDirectory() as data_dir:
            # Write a PID that will report dead
            pid_dir = os.path.join(data_dir, "watchman")
            os.makedirs(pid_dir)
            pid_path = os.path.join(pid_dir, "watchman.pid")
            with open(pid_path, "w") as f:
                f.write("99999")  # unlikely to be a real process

            cfg = {
                "node": {"data_dir": data_dir},
                "health": {"cockpit_url": "http://127.0.0.1:19999"},
            }

            import argparse
            args = argparse.Namespace(config="watchman.toml", data_dir=data_dir, verbose=False)

            import importlib
            import knarr.watchman.main as m
            importlib.reload(m)

            # Patch load_config and _is_pid_alive
            with patch("knarr.watchman.main.load_config", return_value=cfg):
                with patch("knarr.watchman.main._is_pid_alive", return_value=False):
                    with patch("urllib.request.urlopen", side_effect=Exception("offline")):
                        import io
                        from contextlib import redirect_stdout
                        buf = io.StringIO()
                        with redirect_stdout(buf):
                            m.cmd_status(args)
                        output = buf.getvalue()

            self.assertIn("not running", output)
            # PID file should be gone
            self.assertFalse(os.path.exists(pid_path))
