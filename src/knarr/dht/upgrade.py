"""Auto-upgrade: download, verify, and install new Knarr releases.

Opt-in only. SHA256 verification non-negotiable.
Downloads sdist from GitHub releases, verifies against SHA256SUMS,
then pip-installs and signals restart.

N-3: GitHub dependency acknowledged as tech debt.
Future: upgrade-via-DHT (download sdist from any peer, verify SHA256).
"""
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

RELEASE_BASE_URL = "https://github.com/knarrnet/knarr/releases/download"
LATEST_RELEASE_API = "https://api.github.com/repos/knarrnet/knarr/releases/latest"


def get_latest_version() -> Optional[str]:
    """Query GitHub releases API for latest version tag."""
    logger.debug(f"Querying latest version from {LATEST_RELEASE_API}")
    try:
        req = urllib.request.Request(LATEST_RELEASE_API, headers={"User-Agent": "knarr-upgrade"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tag = data.get("tag_name", "")
            return tag.lstrip("v") if tag else None
    except Exception as e:
        logger.debug(f"Failed to fetch latest version: {e}")
        return None


def backup_config(config_dir: str, current_version: str, data_dir: Optional[str] = None) -> Optional[str]:
    """Backup identity + config files before upgrade."""
    if not config_dir:
        return None
    
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_name = f"backup-v{current_version}-{timestamp}"
    backup_path = os.path.join(config_dir, backup_name)
    data_source = data_dir or config_dir
    
    try:
        os.makedirs(backup_path, exist_ok=True)
        files_to_backup = [
            "knarr.toml",
            "node_key.seed",
            "cert.pem",
            "key.pem",
            "cockpit-cert.pem",
            "cockpit-key.pem",
            ".cockpit_token",
            "secrets.toml",
            "node.db",
        ]
        backed_up = []

        # Checkpoint WAL before backup so node.db is self-contained
        db_path = os.path.join(data_source, "node.db")
        if os.path.exists(db_path):
            try:
                import sqlite3
                conn = sqlite3.connect(db_path)
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.close()
            except Exception:
                pass  # best-effort — backup proceeds regardless

        for filename in files_to_backup:
            src_root = config_dir if filename == "knarr.toml" else data_source
            src = os.path.join(src_root, filename)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(backup_path, filename))
                backed_up.append(filename)
        
        if backed_up:
            logger.info(f"Backup created at {backup_path}: {', '.join(backed_up)}")
            return backup_path
        else:
            logger.warning("No files found to backup")
            os.rmdir(backup_path)
            return None
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        return None


def verify_installation(target_version: str) -> bool:
    """Verify installed version matches target after pip install."""
    try:
        # Import knarr in a separate process to avoid module caching issues
        cmd = [sys.executable, "-c", "import knarr; print(knarr.__version__)"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            installed = result.stdout.strip()
            if installed == target_version:
                logger.info(f"Verification success: found v{installed}")
                return True
            else:
                logger.error(f"Verification failure: expected v{target_version}, found v{installed}")
        else:
            logger.error(f"Verification subprocess failed: {result.stderr}")
    except Exception as e:
        logger.error(f"Verification error: {e}")
    return False


def rollback_installation(backup_dir: str, config_dir: str, data_dir: Optional[str] = None) -> bool:
    """Restore config files from backup directory."""
    if not backup_dir or not os.path.exists(backup_dir):
        return False
    
    try:
        restored = []
        for filename in os.listdir(backup_dir):
            src = os.path.join(backup_dir, filename)
            if os.path.isfile(src):
                dest_root = config_dir if filename == "knarr.toml" else (data_dir or config_dir)
                os.makedirs(dest_root, exist_ok=True)
                shutil.copy2(src, os.path.join(dest_root, filename))
                restored.append(filename)
        
        if restored:
            logger.info(f"Rollback: restored {', '.join(restored)} from {backup_dir}")
            return True
    except Exception as e:
        logger.error(f"Rollback failed: {e}")
    return False


def cleanup_old_backups(config_dir: str, retention_days: int = 7):
    """Delete backup directories older than retention_days."""
    if not config_dir or retention_days < 0:
        return
    
    now = time.time()
    cutoff = now - (retention_days * 86400)
    
    try:
        for item in os.listdir(config_dir):
            if item.startswith("backup-v"):
                path = os.path.join(config_dir, item)
                if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                    logger.info(f"Cleaning up old backup: {item}")
                    shutil.rmtree(path)
    except Exception as e:
        logger.warning(f"Failed to cleanup old backups: {e}")


def check_and_upgrade(target_version: str) -> bool:
    """Download, verify, and install a specific version.

    Returns True on success, False on failure.
    NEVER proceeds if checksum verification fails.
    """
    tag = f"v{target_version}"
    logger.info(f"Auto-upgrade: starting upgrade to {tag}")

    try:
        # 1. Fetch SHA256SUMS from release
        sums_url = f"{RELEASE_BASE_URL}/{tag}/SHA256SUMS"
        logger.info(f"Auto-upgrade: fetching checksums from {sums_url}")
        with urllib.request.urlopen(sums_url, timeout=30) as resp:
            sums_text = resp.read().decode("utf-8")

        # Parse SHA256SUMS: each line is "hash  filename"
        expected_hash = None
        sdist_name = f"knarr-{target_version}.tar.gz"
        for line in sums_text.strip().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == sdist_name:
                expected_hash = parts[0]
                break

        if not expected_hash:
            logger.error(f"Auto-upgrade: {sdist_name} not found in SHA256SUMS")
            return False

        # 2. Download sdist to temp file
        sdist_url = f"{RELEASE_BASE_URL}/{tag}/{sdist_name}"
        logger.info(f"Auto-upgrade: downloading {sdist_url}")
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name
            with urllib.request.urlopen(sdist_url, timeout=120) as resp:
                data = resp.read()
                tmp.write(data)

        # 3. Verify SHA256 — ABORT on mismatch (non-negotiable)
        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != expected_hash:
            logger.error(
                f"Auto-upgrade: SHA256 MISMATCH for {sdist_name}! "
                f"Expected {expected_hash}, got {actual_hash}. ABORTING."
            )
            os.unlink(tmp_path)
            return False

        logger.info(f"Auto-upgrade: SHA256 verified for {sdist_name}")

        # 4. pip install
        logger.info(f"Auto-upgrade: installing {tmp_path}")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade",
             "--no-cache-dir", tmp_path],
            capture_output=True, text=True, timeout=300
        )

        os.unlink(tmp_path)

        if result.returncode != 0:
            logger.error(f"Auto-upgrade: pip install failed: {result.stderr}")
            return False

        logger.info(f"Auto-upgrade: successfully installed {tag}")
        return True

    except urllib.error.URLError as e:
        logger.error(f"Auto-upgrade: network error: {e}")
        return False
    except subprocess.TimeoutExpired:
        logger.error("Auto-upgrade: pip install timed out")
        return False
    except Exception as e:
        logger.error(f"Auto-upgrade: unexpected error: {e}")
        return False
