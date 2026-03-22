"""Watchman plugin manager — manifest-driven install, upgrade, enable/disable."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tarfile
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# O-034: plugin names written into TOML section headers must be safe identifiers.
_PLUGIN_NAME_RE = re.compile(r'^[a-zA-Z0-9_.-]+$')

log = logging.getLogger("knarr.watchman.plugins")

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore

try:
    import tomli_w
except ImportError:
    tomli_w = None  # type: ignore

_GITHUB_API = "https://api.github.com"


# ------------------------------------------------------------------
# Manifest I/O
# ------------------------------------------------------------------

def load_manifest(path: str) -> Dict[str, Any]:
    """Load plugins.toml. Returns dict with 'plugins' key."""
    if not os.path.exists(path):
        return {"plugins": {}}
    if tomllib is None:
        raise RuntimeError("TOML parser unavailable — install tomli or use Python >=3.11")
    with open(path, "rb") as f:
        return tomllib.load(f)


def save_manifest(path: str, manifest: Dict[str, Any]) -> None:
    """Write plugins.toml. Requires tomli-w."""
    if tomli_w is None:
        # Fall back to a simple manual serialiser for the common case
        _write_manifest_simple(path, manifest)
        return
    with open(path, "wb") as f:
        tomli_w.dump(manifest, f)


def _write_manifest_simple(path: str, manifest: Dict[str, Any]) -> None:
    """Minimal TOML writer for plugins.toml — no library required."""
    lines = []
    for name, cfg in manifest.get("plugins", {}).items():
        # O-034: reject names that would escape the TOML section header syntax.
        if not _PLUGIN_NAME_RE.match(name):
            raise ValueError(
                f"Invalid plugin name {name!r}: must match [a-zA-Z0-9_-]+"
            )
        lines.append(f"[plugins.{name}]")
        for k, v in cfg.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                escaped = v.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{k} = "{escaped}"')
            else:
                lines.append(f"{k} = {v}")
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))


# ------------------------------------------------------------------
# GitHub release resolution
# ------------------------------------------------------------------

def _resolve_github_release(org: str, repo: str, version_constraint: str) -> Tuple[str, str]:
    """
    Return (tag, tarball_url) for the latest release matching version_constraint.
    version_constraint: '>=1.0.0' or exact 'v1.2.3'.
    """
    url = f"{_GITHUB_API}/repos/{org}/{repo}/releases"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        releases = json.loads(resp.read())

    def _vt(v: str) -> Tuple[int, ...]:
        try:
            return tuple(int(x) for x in v.lstrip("v").split("."))
        except ValueError:
            return (0,)

    constraint_op, constraint_ver = _parse_constraint(version_constraint)
    best_tag: Optional[str] = None
    best_vt: Tuple[int, ...] = (0,)

    for release in releases:
        tag = release.get("tag_name", "")
        vt = _vt(tag)
        if not _satisfies(vt, constraint_op, _vt(constraint_ver)):
            continue
        if vt > best_vt:
            best_vt = vt
            best_tag = tag

    if best_tag is None:
        raise ValueError(f"No release matching '{version_constraint}' found in {org}/{repo}")

    tarball_url = f"https://api.github.com/repos/{org}/{repo}/tarball/{best_tag}"
    return best_tag, tarball_url


def _parse_constraint(constraint: str) -> Tuple[str, str]:
    """Parse '>=1.0.0' → ('>=', '1.0.0') or 'v1.2.3' → ('==', '1.2.3')."""
    for op in (">=", "<=", "==", ">", "<", "!="):
        if constraint.startswith(op):
            return op, constraint[len(op):].lstrip("v")
    return "==", constraint.lstrip("v")


def _satisfies(vt: Tuple[int, ...], op: str, target: Tuple[int, ...]) -> bool:
    if op == ">=":
        return vt >= target
    if op == "<=":
        return vt <= target
    if op == "==":
        return vt == target
    if op == ">":
        return vt > target
    if op == "<":
        return vt < target
    if op == "!=":
        return vt != target
    return False


# ------------------------------------------------------------------
# Install / remove
# ------------------------------------------------------------------

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_tarball(url: str, dest: str) -> None:
    log.info("PLUGIN_DOWNLOAD url=%s", url)
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)


def _extract_tarball(tarball_path: str, plugin_dir: str) -> None:
    """Extract tarball into plugin_dir, stripping the top-level directory."""
    with tarfile.open(tarball_path, "r:gz") as tar:
        members = tar.getmembers()
        if not members:
            return
        # Strip leading component (GitHub tarballs have a top-level org-repo-hash/ dir)
        prefix = members[0].name.split("/")[0] + "/"
        for member in members:
            if member.name.startswith(prefix):
                member.name = member.name[len(prefix):]
            if not member.name:
                continue
            # O-032: reject members whose name (after prefix strip) escapes the
            # plugin directory — e.g. "../../../etc/passwd" or absolute paths.
            parts = [part for part in member.name.split("/") if part]
            if os.path.isabs(member.name) or any(part == ".." for part in parts):
                log.warning("PLUGIN_EXTRACT_SKIP unsafe_member=%r", member.name)
                continue
            # WM-DEPR-01: filter='data' silences the Python 3.12+ DeprecationWarning
            # and adds defence-in-depth path validation at the tarfile layer.
            tar.extract(member, path=plugin_dir, filter="data")


def install_plugin(
    name: str,
    plugin_cfg: Dict[str, Any],
    plugin_root: str,
    staging_dir: str,
) -> str:
    """
    Install a single plugin. Returns installed version tag.
    plugin_root: base directory where plugins live (e.g. data_dir/plugins/)
    """
    # O-034: validate name before it is written into a TOML section header or
    # used as a filesystem path component.
    if not _PLUGIN_NAME_RE.match(name):
        raise ValueError(
            f"Invalid plugin name {name!r}: must match [a-zA-Z0-9_-]+"
        )
    source: str = plugin_cfg.get("source", "")
    version_constraint: str = plugin_cfg.get("version", ">=0.0.0")
    expected_sha256: Optional[str] = plugin_cfg.get("sha256")

    if source.startswith("file://"):
        # Local path install
        local_path = source[len("file://"):]
        dest = os.path.join(plugin_root, name)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(local_path, dest)
        log.info("WATCHMAN_PLUGIN_INSTALL plugin=%s source=local", name)
        return "local"

    if source.startswith("github:"):
        _, slug = source.split(":", 1)
        org, repo = slug.split("/", 1)

        tag, tarball_url = _resolve_github_release(org, repo, version_constraint)
        tarball_name = f"{name}-{tag}.tar.gz"
        tarball_path = os.path.join(staging_dir, tarball_name)

        _download_tarball(tarball_url, tarball_path)

        actual_sha256 = _sha256_file(tarball_path)
        log.info("PLUGIN_VERIFY plugin=%s sha256=%s", name, actual_sha256[:16])
        if expected_sha256 and actual_sha256 != expected_sha256:
            raise ValueError(
                f"SHA256 mismatch for {name}: expected {expected_sha256[:16]} got {actual_sha256[:16]}"
            )

        plugin_dest = os.path.join(plugin_root, name)
        if os.path.exists(plugin_dest):
            shutil.rmtree(plugin_dest)
        os.makedirs(plugin_dest, exist_ok=True)

        _extract_tarball(tarball_path, plugin_dest)
        os.remove(tarball_path)

        log.info("WATCHMAN_PLUGIN_INSTALL plugin=%s version=%s", name, tag)
        return tag

    raise ValueError(f"Unsupported plugin source: {source!r}")


def remove_plugin(name: str, plugin_root: str) -> None:
    dest = os.path.join(plugin_root, name)
    if os.path.exists(dest):
        shutil.rmtree(dest)
        log.info("WATCHMAN_PLUGIN_REMOVE plugin=%s", name)
    else:
        log.warning("PLUGIN_REMOVE_NOTFOUND plugin=%s", name)


def get_installed_version(name: str, plugin_root: str) -> Optional[str]:
    """Return installed version from plugin.toml, or None if not installed."""
    plugin_toml = os.path.join(plugin_root, name, "plugin.toml")
    if not os.path.exists(plugin_toml):
        return None
    if tomllib is None:
        return "unknown"
    try:
        with open(plugin_toml, "rb") as f:
            data = tomllib.load(f)
        return data.get("plugin", {}).get("version") or data.get("version")
    except Exception:
        return "unknown"


# ------------------------------------------------------------------
# PluginManager
# ------------------------------------------------------------------

class PluginManager:
    """Reconciles installed plugins against plugins.toml manifest."""

    def __init__(self, cfg: Dict[str, Any]):
        self._cfg = cfg
        data_dir = cfg["node"].get("data_dir", ".")
        plugins_cfg = cfg.get("plugins", {})
        plugin_dir = plugins_cfg.get("plugin_dir", "plugins")

        self._manifest_path = os.path.join(data_dir, "watchman", "plugins.toml")
        self._plugin_root = os.path.join(data_dir, plugin_dir)
        self._staging_dir = os.path.join(data_dir, "watchman", "staging", "plugins")
        os.makedirs(self._plugin_root, exist_ok=True)
        os.makedirs(self._staging_dir, exist_ok=True)

    def sync(self) -> None:
        """Install/upgrade all plugins declared in plugins.toml."""
        manifest = load_manifest(self._manifest_path)
        plugins = manifest.get("plugins", {})
        if not plugins:
            log.info("PLUGIN_SYNC no plugins declared")
            return

        for name, plugin_cfg in plugins.items():
            if not plugin_cfg.get("enabled", True):
                log.debug("PLUGIN_SYNC_SKIP plugin=%s enabled=false", name)
                continue
            installed = get_installed_version(name, self._plugin_root)
            source = plugin_cfg.get("source", "")
            # Simple check: if not installed, install. Version upgrade check is future scope.
            if installed is None:
                try:
                    install_plugin(name, plugin_cfg, self._plugin_root, self._staging_dir)
                except Exception as e:
                    log.error("PLUGIN_INSTALL_FAIL plugin=%s error=%s", name, e)
            else:
                log.debug("PLUGIN_SYNC_OK plugin=%s version=%s", name, installed)

    def install(self, name: str) -> None:
        manifest = load_manifest(self._manifest_path)
        plugin_cfg = manifest.get("plugins", {}).get(name)
        if not plugin_cfg:
            raise ValueError(f"Plugin '{name}' not in manifest ({self._manifest_path})")
        install_plugin(name, plugin_cfg, self._plugin_root, self._staging_dir)

    def remove(self, name: str) -> None:
        remove_plugin(name, self._plugin_root)

    def enable(self, name: str) -> None:
        self._set_enabled(name, True)

    def disable(self, name: str) -> None:
        self._set_enabled(name, False)

    def _set_enabled(self, name: str, enabled: bool) -> None:
        manifest = load_manifest(self._manifest_path)
        if name not in manifest.get("plugins", {}):
            raise ValueError(f"Plugin '{name}' not in manifest")
        manifest["plugins"][name]["enabled"] = enabled
        save_manifest(self._manifest_path, manifest)
        log.info("PLUGIN_%s plugin=%s", "ENABLE" if enabled else "DISABLE", name)

    def list_plugins(self) -> List[Dict[str, Any]]:
        manifest = load_manifest(self._manifest_path)
        result = []
        for name, cfg in manifest.get("plugins", {}).items():
            installed = get_installed_version(name, self._plugin_root)
            result.append({
                "name": name,
                "source": cfg.get("source", ""),
                "enabled": cfg.get("enabled", True),
                "installed_version": installed or "not installed",
            })
        return result
