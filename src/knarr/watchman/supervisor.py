"""Watchman supervisor — spawns, monitors, and recovers the knarr node process."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

log = logging.getLogger("knarr.watchman")


class Supervisor:
    """
    Owns the knarr node lifecycle:
    - spawns the node subprocess
    - probes health on a fixed interval
    - restarts on crash with exponential backoff
    - emits structured WATCHMAN_* log events
    """

    def __init__(self, cfg: Dict[str, Any]):
        self._cfg = cfg
        self._node_cfg = cfg["node"]
        self._health_cfg = cfg["health"]
        self._recovery_cfg = cfg["recovery"]

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._start_time: float = 0.0
        self._restart_count: int = 0
        self._consecutive_health_fails: int = 0
        self._running: bool = False
        self._shutdown_requested: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main supervisor loop. Returns when stop() is called."""
        self._running = True
        self._shutdown_requested = False

        # Sync plugins before starting the node (if enabled)
        plugins_cfg = self._cfg.get("plugins", {})
        if plugins_cfg.get("sync_plugins", False):
            try:
                from .plugin_manager import PluginManager
                pm = PluginManager(self._cfg)
                pm.sync()
            except Exception as e:
                log.warning("WATCHMAN_PLUGIN_SYNC_FAIL error=%s — continuing", e)

        await self._spawn()

        health_task = asyncio.create_task(self._health_loop())
        monitor_task = asyncio.create_task(self._monitor_loop())
        upgrade_task = asyncio.create_task(self._upgrade_loop())

        try:
            await asyncio.gather(health_task, monitor_task, upgrade_task)
        except asyncio.CancelledError:
            pass
        finally:
            await self._terminate()

    async def stop(self) -> None:
        """Request clean shutdown."""
        self._shutdown_requested = True
        self._running = False
        await self._terminate()

    def get_status(self) -> Dict[str, Any]:
        """Return current supervisor status dict."""
        uptime = time.monotonic() - self._start_time if self._start_time else 0
        return {
            "node_running": self._proc is not None and self._proc.returncode is None,
            "node_pid": self._proc.pid if self._proc else None,
            "uptime_seconds": int(uptime),
            "restart_count": self._restart_count,
            "health_fails": self._consecutive_health_fails,
        }

    # ------------------------------------------------------------------
    # Spawn / terminate
    # ------------------------------------------------------------------

    async def _spawn(self) -> None:
        command = self._node_cfg["command"]
        args = list(self._node_cfg.get("args", []))

        # If command is a bare module name, run as python -m
        if command in ("knarr",) and not os.path.isfile(command):
            argv = [sys.executable, "-m", "knarr"] + args
        else:
            argv = [command] + args

        log.info(
            "WATCHMAN_START cmd=%s args=%s restart=%d",
            command, " ".join(args), self._restart_count,
        )

        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=None,  # inherit — node writes its own logs
            stderr=None,
        )
        self._start_time = time.monotonic()

    async def _terminate(self) -> None:
        if self._proc is None or self._proc.returncode is not None:
            return
        log.info("WATCHMAN_STOP reason=shutdown pid=%d", self._proc.pid)
        try:
            if sys.platform == "win32":
                self._proc.terminate()
            else:
                self._proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        except ProcessLookupError:
            pass

    # ------------------------------------------------------------------
    # Monitor loop — watches for unexpected exits
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        while self._running:
            if self._proc is not None:
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue  # still running

                if self._shutdown_requested:
                    break

                exit_code = self._proc.returncode
                uptime = time.monotonic() - self._start_time
                log.warning(
                    "WATCHMAN_CRASH exit_code=%d uptime=%.1fs restart_attempt=%d",
                    exit_code, uptime, self._restart_count,
                )

                await self._restart()
            else:
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Crash recovery with exponential backoff
    # ------------------------------------------------------------------

    async def _restart(self) -> None:
        max_restarts = self._recovery_cfg["max_restarts"]
        initial_backoff = self._recovery_cfg["initial_backoff"]
        max_backoff = self._recovery_cfg["max_backoff"]
        reset_uptime = self._recovery_cfg["backoff_reset_uptime"]

        uptime = time.monotonic() - self._start_time
        if uptime >= reset_uptime:
            self._restart_count = 0
            log.info("WATCHMAN_BACKOFF_RESET uptime=%.1fs", uptime)

        self._restart_count += 1

        if self._restart_count > max_restarts:
            log.error(
                "WATCHMAN_GIVE_UP restarts=%d max=%d",
                self._restart_count, max_restarts,
            )
            self._running = False
            return

        backoff = min(initial_backoff * (2 ** (self._restart_count - 1)), max_backoff)
        log.info("WATCHMAN_RESTART attempt=%d backoff=%.0fs", self._restart_count, backoff)
        await asyncio.sleep(backoff)

        await self._spawn()

    # ------------------------------------------------------------------
    # Health loop
    # ------------------------------------------------------------------

    async def _upgrade_loop(self) -> None:
        """Periodic auto-upgrade check. Only runs if auto_upgrade=true."""
        upgrade_cfg = self._cfg.get("upgrade", {})
        if not upgrade_cfg.get("auto_upgrade", False):
            return

        check_interval = upgrade_cfg.get("check_interval", 3600)
        await asyncio.sleep(60)  # initial delay — let node stabilise first

        from .upgrader import Upgrader
        upgrader = Upgrader(self._cfg, self)

        while self._running:
            try:
                log.info("WATCHMAN_UPGRADE_CHECK")
                await upgrader.run_upgrade()
            except Exception as e:
                log.warning("WATCHMAN_UPGRADE_ERROR error=%s", e)
            await asyncio.sleep(check_interval)

    async def _health_loop(self) -> None:
        interval = self._health_cfg["health_interval"]
        fail_threshold = self._health_cfg["health_fail_threshold"]

        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break

            healthy = await self._probe_health()

            if not healthy:
                self._consecutive_health_fails += 1
                log.warning(
                    "WATCHMAN_HEALTH_FAIL consecutive=%d threshold=%d",
                    self._consecutive_health_fails, fail_threshold,
                )
                if self._consecutive_health_fails >= fail_threshold:
                    log.error(
                        "WATCHMAN_HEALTH_FAIL_CRITICAL consecutive=%d — killing node",
                        self._consecutive_health_fails,
                    )
                    if self._proc and self._proc.returncode is None:
                        self._proc.kill()
            else:
                if self._consecutive_health_fails > 0:
                    log.info(
                        "WATCHMAN_HEALTH_RECOVER after=%d failures",
                        self._consecutive_health_fails,
                    )
                self._consecutive_health_fails = 0

    async def _probe_health(self) -> bool:
        """Try TCP connect first (fast), then HTTP /health. Returns True if healthy."""
        cockpit_url = self._health_cfg["cockpit_url"]

        # Parse host/port from cockpit_url
        try:
            from urllib.parse import urlparse
            parsed = urlparse(cockpit_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 8080
        except Exception:
            host, port = "127.0.0.1", 8080

        # Probe 1: TCP connect
        try:
            loop = asyncio.get_running_loop()
            conn = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(conn, timeout=2.0)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        except Exception:
            return False

        # Probe 2: HTTP GET /health
        health_url = f"{cockpit_url}/health"
        try:
            def _fetch():
                req = urllib.request.Request(health_url)
                with urllib.request.urlopen(req, timeout=3) as resp:
                    return resp.status == 200
            result = await loop.run_in_executor(None, _fetch)
            return result
        except Exception:
            return False
