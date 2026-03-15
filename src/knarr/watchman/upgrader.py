"""Watchman upgrader — staged upgrade with SHA256 verify, drain, and rollback."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("knarr.watchman.upgrade")

_GITHUB_API = "https://api.github.com"


def _parse_source(source: str) -> Tuple[str, str]:
    """Parse 'github:org/repo' → (org, repo). Only github: scheme supported."""
    if not source.startswith("github:"):
        raise ValueError(f"Unsupported upgrade source: {source!r} (only 'github:org/repo' supported)")
    _, slug = source.split(":", 1)
    parts = slug.split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid github source: {source!r} — expected 'github:org/repo'")
    return parts[0], parts[1]


def _get_running_version() -> str:
    """Return the currently installed knarr version string."""
    try:
        import knarr
        return getattr(knarr, "__version__", "0.0.0")
    except ImportError:
        return "0.0.0"


def _version_tuple(v: str) -> Tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.lstrip("v").split("."))
    except ValueError:
        return (0,)


def _fetch_latest_release(org: str, repo: str) -> Optional[Dict[str, Any]]:
    """Fetch latest release info from GitHub API. Returns None on error."""
    url = f"{_GITHUB_API}/repos/{org}/{repo}/releases/latest"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning("UPGRADE_CHECK_FAIL url=%s error=%s", url, e)
        return None


def _download_file(url: str, dest: str) -> None:
    """Download url to dest path, logging progress."""
    log.info("UPGRADE_DOWNLOAD url=%s dest=%s", url, dest)
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _install_from_tarball(tarball_path: str) -> None:
    """Install knarr from a source tarball using pip."""
    log.info("UPGRADE_INSTALL tarball=%s", tarball_path)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", tarball_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pip install failed:\n{result.stderr}")
    log.info("UPGRADE_INSTALL_OK output=%s", result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "ok")


def _install_from_tag(org: str, repo: str, tag: str) -> None:
    """Install knarr directly from a GitHub tag via pip (fallback if no wheel in release)."""
    url = f"git+https://github.com/{org}/{repo}.git@{tag}"
    log.info("UPGRADE_INSTALL_GIT tag=%s", tag)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", url],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pip install from git failed:\n{result.stderr}")
    log.info("UPGRADE_INSTALL_OK")


class Upgrader:
    """
    Staged upgrade controller.

    Flow: CHECK → DOWNLOAD → VERIFY → BACKUP → DRAIN → SWAP → HEALTH → PASS/FAIL
    """

    def __init__(self, cfg: Dict[str, Any], supervisor: Any):
        self._cfg = cfg
        self._upgrade_cfg = cfg["upgrade"]
        self._health_cfg = cfg["health"]
        self._supervisor = supervisor  # back-ref for drain + restart

    def check_available(self) -> Optional[str]:
        """Return latest available version tag if newer than running, else None."""
        source = self._upgrade_cfg["source"]
        org, repo = _parse_source(source)
        release = _fetch_latest_release(org, repo)
        if not release:
            return None

        tag = release.get("tag_name", "")
        latest = tag.lstrip("v")
        current = _get_running_version()

        if _version_tuple(latest) > _version_tuple(current):
            log.info("UPGRADE_AVAILABLE current=%s latest=%s", current, latest)
            return tag
        log.debug("UPGRADE_CURRENT current=%s latest=%s — no update needed", current, latest)
        return None

    async def run_upgrade(self, tag: Optional[str] = None) -> bool:
        """
        Execute the full staged upgrade flow. Returns True on success, False on rollback.
        If tag is None, checks for latest and upgrades to it.
        """
        source = self._upgrade_cfg["source"]
        org, repo = _parse_source(source)
        drain_timeout = self._upgrade_cfg["drain_timeout"]
        health_timeout = self._upgrade_cfg.get("upgrade_health_timeout",
                                                self._upgrade_cfg["health_timeout"])
        current_version = _get_running_version()

        # --- CHECK ---
        if tag is None:
            tag = self.check_available()
            if not tag:
                log.info("UPGRADE_SKIP already_current=%s", current_version)
                return True

        new_version = tag.lstrip("v")
        log.info("UPGRADE_START from=%s to=%s", current_version, new_version)

        # --- DOWNLOAD ---
        staging_dir = os.path.join(
            self._cfg["node"].get("data_dir", "."), "watchman", "staging", new_version
        )
        os.makedirs(staging_dir, exist_ok=True)

        release = _fetch_latest_release(org, repo)
        tarball_path = None
        expected_sha256 = None

        if release:
            assets = release.get("assets", [])

            # O-033: Pass 1 — collect checksums before downloading any asset.
            # Single-pass broke when checksums.txt appeared after the .whl in
            # the asset list; the loop hit `break` before reaching checksums.txt.
            for asset in assets:
                if asset.get("name", "").lower() == "checksums.txt":
                    try:
                        req = urllib.request.Request(asset["browser_download_url"])
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            checksums_text = resp.read().decode()
                        for line in checksums_text.splitlines():
                            parts = line.split()
                            if len(parts) == 2 and parts[1].endswith(".whl"):
                                expected_sha256 = parts[0]
                    except Exception:
                        pass
                    break

            # O-033: Pass 2 — download the wheel now that we have the checksum.
            # O-031: os.path.basename() prevents path traversal via crafted asset names.
            for asset in assets:
                name: str = asset.get("name", "")
                if name.endswith(".whl") and "knarr" in name.lower():
                    safe_name = os.path.basename(name)  # O-031: strip any directory components
                    tarball_path = os.path.join(staging_dir, safe_name)
                    try:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(
                            None, _download_file, asset["browser_download_url"], tarball_path
                        )
                    except Exception as e:
                        log.warning("UPGRADE_DOWNLOAD_FAIL asset=%s error=%s", safe_name, e)
                        tarball_path = None
                    break

        # --- VERIFY ---
        if tarball_path and os.path.exists(tarball_path):
            actual_sha256 = _sha256_file(tarball_path)
            log.info("UPGRADE_VERIFY file=%s sha256=%s", os.path.basename(tarball_path), actual_sha256[:16])
            if expected_sha256 and actual_sha256 != expected_sha256:
                log.error("UPGRADE_VERIFY_FAIL expected=%s actual=%s", expected_sha256[:16], actual_sha256[:16])
                return False

        # --- BACKUP ---
        backup_dir = os.path.join(
            self._cfg["node"].get("data_dir", "."), "watchman", "backup", current_version
        )
        os.makedirs(backup_dir, exist_ok=True)
        # Record the version we're rolling back to
        with open(os.path.join(backup_dir, "version.txt"), "w") as f:
            f.write(current_version)
        log.info("UPGRADE_BACKUP version=%s dir=%s", current_version, backup_dir)

        # --- DRAIN ---
        log.info("UPGRADE_DRAIN timeout=%ds", drain_timeout)
        await self._drain(drain_timeout)

        # --- SWAP ---
        log.info("UPGRADE_SWAP stopping_node")
        await self._supervisor._terminate()

        try:
            loop = asyncio.get_running_loop()
            if tarball_path and os.path.exists(tarball_path):
                await loop.run_in_executor(None, _install_from_tarball, tarball_path)
            else:
                await loop.run_in_executor(None, _install_from_tag, org, repo, tag)
        except Exception as e:
            log.error("UPGRADE_SWAP_FAIL error=%s — rolling back", e)
            await self._rollback(org, repo, current_version, backup_dir)
            return False

        # Start new version — detached so it survives asyncio.run() teardown
        await self._supervisor._spawn(detached=True)

        # --- HEALTH ---
        log.info("UPGRADE_HEALTH_CHECK timeout=%ds", health_timeout)
        deadline = time.monotonic() + health_timeout
        healthy = False
        while time.monotonic() < deadline:
            healthy = await self._supervisor._probe_health()
            if healthy:
                break
            await asyncio.sleep(2)

        if not healthy:
            log.error("UPGRADE_ROLLBACK reason=health_check_failed after=%ds", health_timeout)
            await self._supervisor._terminate()
            await self._rollback(org, repo, current_version, backup_dir)
            return False

        # --- PASS ---
        log.info("UPGRADE_SUCCESS from=%s to=%s", current_version, new_version)
        shutil.rmtree(staging_dir, ignore_errors=True)
        shutil.rmtree(backup_dir, ignore_errors=True)
        return True

    async def _drain(self, timeout: int) -> None:
        """Wait for running tasks to complete, up to timeout seconds."""
        cockpit_url = self._health_cfg["cockpit_url"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                loop = asyncio.get_running_loop()
                def _check():
                    import urllib.request as _ur
                    with _ur.urlopen(f"{cockpit_url}/api/tasks", timeout=3) as r:
                        tasks = json.loads(r.read())
                        running = [t for t in (tasks if isinstance(tasks, list) else [])
                                   if t.get("status") in ("running", "queued")]
                        return len(running)
                running_count = await loop.run_in_executor(None, _check)
                if running_count == 0:
                    log.info("UPGRADE_DRAIN_DONE tasks_cleared=True")
                    return
                log.info("UPGRADE_DRAIN_WAIT running_tasks=%d", running_count)
            except Exception:
                pass
            await asyncio.sleep(3)
        log.warning("UPGRADE_DRAIN_TIMEOUT timeout=%ds — proceeding anyway", timeout)

    async def _rollback(self, org: str, repo: str, version: str, backup_dir: str) -> None:
        log.warning("UPGRADE_ROLLBACK rolling_back_to=%s", version)
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, _install_from_tag, org, repo, f"v{version}"
            )
        except Exception as e:
            log.error("UPGRADE_ROLLBACK_FAIL error=%s", e)
            return

        await self._supervisor._spawn(detached=True)
        log.info("UPGRADE_ROLLBACK_DONE version=%s", version)
