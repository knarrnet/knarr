"""Watchman init — bootstrap a full knarr node from scratch.

Steps: detect Python -> create dirs -> create venv -> install knarr ->
       generate identity/wallet/vault -> write configs -> Thrall prompt -> summary.

Idempotent: safe to re-run. Skips existing identity, configs, venv.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("knarr.watchman.init")

# Minimum Python version for knarr
MIN_PYTHON = (3, 11)

# Default config values
DEFAULT_PORT = 9010
DEFAULT_COCKPIT = 8080
BOOTSTRAP_PEERS = [
    "bootstrap1.knarr.network:9000",
    "bootstrap2.knarr.network:9000",
]
GITHUB_RELEASES_URL = "https://api.github.com/repos/knarrnet/knarr/releases/latest"


# ------------------------------------------------------------------
# Python detection
# ------------------------------------------------------------------

def detect_python() -> Optional[str]:
    """Find a Python >= 3.11 interpreter. Platform-branched detection.

    Windows: tries ``py -3`` then ``python``.
    Unix/macOS: tries ``python3`` then ``python``.

    Returns:
        The command string for a suitable Python (e.g. "py -3", "python3"),
        or None if no suitable Python is found.
    """
    if sys.platform == "win32":
        candidates = ["py -3", "python"]
    else:
        candidates = ["python3", "python"]

    for candidate in candidates:
        try:
            result = subprocess.run(
                candidate.split() + ["--version"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                continue
            version_str = result.stdout.strip()  # "Python 3.12.0"
            parts = version_str.split()
            if len(parts) < 2:
                continue
            ver_parts = parts[1].split(".")
            major, minor = int(ver_parts[0]), int(ver_parts[1])
            if (major, minor) >= MIN_PYTHON:
                log.debug("INIT_PYTHON_FOUND candidate=%s version=%s",
                          candidate, version_str)
                return candidate
            else:
                log.debug("INIT_PYTHON_OLD candidate=%s version=%d.%d need>=%d.%d",
                          candidate, major, minor, *MIN_PYTHON)
        except (subprocess.TimeoutExpired, FileNotFoundError,
                OSError, ValueError, IndexError):
            continue

    log.error("INIT_PYTHON_NONE tried=%s", candidates)
    return None


# ------------------------------------------------------------------
# Venv / pip helpers
# ------------------------------------------------------------------

def _pip_path(data_dir: str) -> str:
    """Return path to pip inside the venv."""
    if sys.platform == "win32":
        return os.path.join(data_dir, ".venv", "Scripts", "pip.exe")
    return os.path.join(data_dir, ".venv", "bin", "pip")


def _python_in_venv(data_dir: str) -> str:
    """Return path to python inside the venv."""
    if sys.platform == "win32":
        return os.path.join(data_dir, ".venv", "Scripts", "python.exe")
    return os.path.join(data_dir, ".venv", "bin", "python")


# ------------------------------------------------------------------
# GitHub / wheel helpers
# ------------------------------------------------------------------

def _fetch_wheel_url(github_token: Optional[str] = None) -> str:
    """Query GitHub releases API for the latest knarr wheel URL.

    Returns the browser_download_url of the .whl asset.
    Raises RuntimeError on failure (network, rate-limit, no asset).
    """
    import urllib.request
    import urllib.error

    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    req = urllib.request.Request(GITHUB_RELEASES_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise RuntimeError(
                "GitHub API rate limit exceeded. "
                "Use --github-token to authenticate, or --wheel for offline install."
            ) from e
        raise RuntimeError(f"GitHub API error {e.code}: {e.reason}") from e
    except Exception as e:
        raise RuntimeError(f"Failed to query GitHub releases: {e}") from e

    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.endswith(".whl") and "knarr" in name.lower():
            url = asset["browser_download_url"]
            log.info("INIT_WHEEL_FOUND name=%s url=%s", name, url)
            return url

    raise RuntimeError(
        "No .whl asset found in latest release. "
        "Use --wheel <path> to install from a local wheel."
    )


def _install_knarr(data_dir: str, wheel: Optional[str] = None,
                    github_token: Optional[str] = None) -> str:
    """Install knarr into the venv. Returns installed version string."""
    pip = _pip_path(data_dir)

    if wheel:
        source = os.path.abspath(wheel)
        log.info("INIT_INSTALL source=local wheel=%s", source)
    else:
        source = _fetch_wheel_url(github_token)
        log.info("INIT_INSTALL source=github url=%s", source)

    result = subprocess.run(
        [pip, "install", source],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pip install failed:\n{result.stderr}")

    # Query installed version
    result = subprocess.run(
        [pip, "show", "knarr"],
        capture_output=True, text=True,
    )
    version = "unknown"
    for line in result.stdout.splitlines():
        if line.startswith("Version:"):
            version = line.split(":", 1)[1].strip()
            break

    log.info("INIT_INSTALL_OK version=%s", version)
    return version


# ------------------------------------------------------------------
# Identity generation
# ------------------------------------------------------------------

def _remove_sqlite_artifacts(path: str) -> None:
    """Remove a SQLite database and its WAL/SHM journal files."""
    for candidate in (path, f"{path}-wal", f"{path}-shm"):
        try:
            os.remove(candidate)
            log.debug("INIT_FORCE_DELETE file=%s", candidate)
        except FileNotFoundError:
            continue


def _generate_identity(data_dir: str, force: bool = False) -> tuple:
    """Generate or load Ed25519 identity, Solana wallet, and vault.

    Returns:
        (node_id_hex, wallet_address) tuple.
    Raises:
        RuntimeError on partial install (one of node.db/vault.db missing).
    """
    node_db = os.path.join(data_dir, "node.db")
    vault_db = os.path.join(data_dir, "vault.db")

    node_exists = os.path.exists(node_db)
    vault_exists = os.path.exists(vault_db)

    # --force: delete both (including WAL/SHM) and regenerate
    if force:
        _remove_sqlite_artifacts(node_db)
        _remove_sqlite_artifacts(vault_db)
        node_exists = vault_exists = False
        log.info("INIT_FORCE_REGENERATE data_dir=%s", data_dir)

    # Both exist — load existing identity
    if node_exists and vault_exists:
        from knarr.dht.storage import Storage
        storage = Storage(node_db)
        try:
            key_bytes = storage.get_node_key()
        finally:
            storage.close()
        if key_bytes:
            from nacl.signing import SigningKey
            sk = SigningKey(key_bytes)
            node_id = hashlib.sha256(sk.verify_key.encode()).hexdigest()
            from knarr.core.wallet import derive_solana_address
            wallet = derive_solana_address(sk)
            log.info("INIT_IDENTITY_EXISTS node_id=%s", node_id[:16])
            return node_id, wallet

    # Partial install — one exists without the other
    if node_exists != vault_exists:
        raise RuntimeError(
            "Partial installation detected. "
            f"node.db={'exists' if node_exists else 'missing'}, "
            f"vault.db={'exists' if vault_exists else 'missing'}. "
            "Both node.db and vault.db must be present. "
            "Use --force to regenerate both."
        )

    # Neither exists — generate fresh identity
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    seed = sk.encode()  # 32-byte seed
    node_id = hashlib.sha256(sk.verify_key.encode()).hexdigest()

    # Store seed in node.db
    from knarr.dht.storage import Storage
    storage = Storage(node_db)
    try:
        storage.set_node_key(seed)
    finally:
        storage.close()
    log.info("INIT_IDENTITY_CREATED node_id=%s", node_id[:16])

    # Derive Solana wallet address
    from knarr.core.wallet import derive_solana_address
    wallet = derive_solana_address(sk)
    log.info("INIT_WALLET wallet=%s", wallet[:16])

    # Create encrypted vault (key derives from signing key seed — no password)
    from knarr.core.vault import KeyringVault
    vault = KeyringVault(vault_db, seed)
    vault.close()
    log.info("INIT_VAULT_CREATED path=%s", vault_db)

    return node_id, wallet


# ------------------------------------------------------------------
# Config generation
# ------------------------------------------------------------------

def _write_knarr_toml(data_dir: str) -> bool:
    """Write knarr.toml with default settings. Skips if file exists.

    Returns True if written, False if skipped.
    """
    path = os.path.join(data_dir, "knarr.toml")
    if os.path.exists(path):
        log.debug("INIT_CONFIG_SKIP file=knarr.toml reason=exists")
        return False

    content = f"""\
[node]
port = {DEFAULT_PORT}
host = "0.0.0.0"
storage = "node.db"

[network]
bootstrap = [
    "{BOOTSTRAP_PEERS[0]}",
    "{BOOTSTRAP_PEERS[1]}",
]

[cockpit]
port = {DEFAULT_COCKPIT}
"""
    with open(path, "w") as f:
        f.write(content)
    log.info("INIT_CONFIG_WRITE file=knarr.toml port=%d cockpit=%d",
             DEFAULT_PORT, DEFAULT_COCKPIT)
    return True


def _write_watchman_toml(data_dir: str) -> bool:
    """Write watchman.toml with default settings. Skips if file exists.

    Returns True if written, False if skipped.
    """
    path = os.path.join(data_dir, "watchman.toml")
    if os.path.exists(path):
        log.debug("INIT_CONFIG_SKIP file=watchman.toml reason=exists")
        return False

    # Use forward slashes for TOML compatibility on Windows
    data_dir_toml = data_dir.replace("\\", "/")

    # Use venv python + module invocation so the command works regardless
    # of whether knarr is on system PATH (standalone binary installs to venv)
    venv_python = _python_in_venv(data_dir).replace("\\", "/")

    content = f"""\
[node]
command = "{venv_python}"
args = ["-m", "knarr", "serve"]
data_dir = "{data_dir_toml}"

[health]
cockpit_url = "http://127.0.0.1:{DEFAULT_COCKPIT}"

[upgrade]
source = "github:knarrnet/knarr"
auto_upgrade = false

[plugins]
plugin_dir = "plugins"
"""
    with open(path, "w") as f:
        f.write(content)
    log.info("INIT_CONFIG_WRITE file=watchman.toml")
    return True


def _write_plugins_toml(data_dir: str, enable_thrall: bool) -> bool:
    """Write watchman/plugins.toml. Skips if file exists.

    Returns True if written, False if skipped.
    """
    path = os.path.join(data_dir, "watchman", "plugins.toml")
    if os.path.exists(path):
        log.debug("INIT_CONFIG_SKIP file=plugins.toml reason=exists")
        return False

    os.makedirs(os.path.dirname(path), exist_ok=True)

    enabled_str = "true" if enable_thrall else "false"
    content = f"""\
[plugins.thrall]
source = "github:knarrnet/knarr-thrall"
enabled = {enabled_str}
"""
    with open(path, "w") as f:
        f.write(content)
    log.info("INIT_PLUGINS_WRITE thrall_enabled=%s", enable_thrall)
    return True


# ------------------------------------------------------------------
# Main init orchestrator
# ------------------------------------------------------------------

def run_init(
    data_dir: str,
    force: bool = False,
    enable_thrall: Optional[bool] = None,
    github_token: Optional[str] = None,
    wheel: Optional[str] = None,
    input_func: Callable = input,
) -> dict[str, Any]:
    """Run the full watchman init sequence.

    Args:
        data_dir: Target data directory (required).
        force: If True, regenerate identity/vault even if they exist.
        enable_thrall: True=enable, False=disable, None=prompt interactively.
        github_token: GitHub API token for authenticated requests.
        wheel: Path to local .whl file for air-gapped install.
        input_func: Callable for interactive prompts (injectable for tests).

    Returns:
        Dict with node_id, wallet, version, data_dir, thrall_enabled.
    """
    data_dir = os.path.abspath(data_dir)
    log.info("INIT_START data_dir=%s force=%s", data_dir, force)

    # 1. Check Python
    python_cmd = detect_python()
    if python_cmd is None:
        raise RuntimeError(
            f"No Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]} found. "
            "Install Python 3.11+ and ensure it is on PATH."
        )
    print(f"Python: {python_cmd}")

    # 2. Create directory structure
    for subdir in ("models", "plugins", "watchman"):
        os.makedirs(os.path.join(data_dir, subdir), exist_ok=True)
    log.info("INIT_DIRS data_dir=%s", data_dir)

    # 3. Create venv
    venv_dir = os.path.join(data_dir, ".venv")
    if not os.path.exists(venv_dir):
        print("Creating virtual environment...")
        result = subprocess.run(
            python_cmd.split() + ["-m", "venv", venv_dir],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to create virtual environment at {venv_dir}:\n"
                f"{result.stderr}"
            )
        log.info("INIT_VENV_CREATED path=%s", venv_dir)
    else:
        log.debug("INIT_VENV_SKIP reason=exists")

    # 4. Install knarr
    print("Installing knarr...")
    version = _install_knarr(data_dir, wheel=wheel, github_token=github_token)
    print(f"Installed knarr {version}")

    # 5. Identity
    print("Checking identity...")
    node_id, wallet_addr = _generate_identity(data_dir, force=force)

    # 6. Config files (skip if present)
    _write_knarr_toml(data_dir)
    _write_watchman_toml(data_dir)

    # 7. Thrall prompt
    if enable_thrall is None:
        try:
            answer = input_func(
                "Enable Thrall intelligence layer? "
                "(~800MB model download) [y/N] "
            )
            thrall = answer.strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            thrall = False
    else:
        thrall = enable_thrall
    _write_plugins_toml(data_dir, thrall)

    # 8. Summary
    print()
    print("=" * 60)
    print("Node initialized successfully!")
    print(f"  Node ID:   {node_id}")
    print(f"  Wallet:    {wallet_addr}")
    print(f"  Version:   {version}")
    print(f"  Data dir:  {data_dir}")
    print()
    print("WARNING: Back up node.db and vault.db — these contain your")
    print("node identity and encrypted secrets and cannot be recovered")
    print("if lost.")
    print()
    # Point to the venv's knarr-watchman script (works even if not on system PATH)
    if sys.platform == "win32":
        venv_watchman = os.path.join(data_dir, ".venv", "Scripts", "knarr-watchman.exe")
    else:
        venv_watchman = os.path.join(data_dir, ".venv", "bin", "knarr-watchman")
    print("To start your node:")
    print(f"  \"{venv_watchman}\" --data-dir \"{data_dir}\" run")
    print("=" * 60)

    return {
        "data_dir": data_dir,
        "node_id": node_id,
        "wallet": wallet_addr,
        "version": version,
        "thrall_enabled": thrall,
    }
