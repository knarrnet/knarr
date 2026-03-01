"""knarr-static skill handler: agent-deployed web frontends.

Actions:
    deploy   — deploy a static site from a zip archive (local only)
    undeploy — remove a deployed site (local only)
    list     — list deployed sites (local only)
"""
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Module-level node reference, injected via set_node()
_node = None
_static_config: Dict[str, Any] = {}
_deployments: Dict[str, Dict[str, Any]] = {}  # path -> {files, deployed_at, archive_hash}
_deployments_lock = threading.Lock()  # V011-008: thread-safe access

MAX_DEPLOYMENTS = 50
MAX_EXTRACTED_SIZE = 200 * 1024 * 1024  # 200 MB
MAX_FILES = 1000
PATH_PATTERN = re.compile(r'^[a-z0-9][a-z0-9-/]*[a-z0-9]$|^[a-z0-9]$')
MAX_PATH_LENGTH = 64


def set_node(node):
    """Called by the node framework to inject the DHTNode reference."""
    global _node, _static_config, _deployments
    _node = node
    _static_config = node._config.get("static", {})
    # Scan for existing deployments
    static_root = _get_static_root()
    if static_root.exists():
        for child in static_root.iterdir():
            if child.is_dir():
                files = sum(1 for _ in child.rglob("*") if _.is_file())
                _deployments[child.name] = {
                    "files": files,
                    "deployed_at": child.stat().st_mtime,
                    "archive_hash": "",
                }


def _get_static_root() -> Path:
    """Returns the root directory for static deployments."""
    if _node and _node._asset_dir:
        return Path(_node._asset_dir).parent / "static"
    return Path("static")


def handle(input_data: dict) -> dict:
    """Main handler dispatching to deploy/undeploy/list actions."""
    if _node is None:
        return {"error": "static_not_initialized", "message": "Static handler not initialized"}

    # Local-only: check caller identity
    caller = input_data.get("_caller_node_id", "")
    if caller != _node.node_info.node_id:
        return {"error": "local_only", "message": "knarr-static is local-only"}

    action = input_data.get("action", "")
    if action == "deploy":
        return _handle_deploy(input_data)
    elif action == "undeploy":
        return _handle_undeploy(input_data)
    elif action == "list":
        return _handle_list()
    return {"error": "unknown_action", "message": f"Unknown action: {action}"}


def _handle_deploy(input_data: dict) -> dict:
    """Deploy a static site from a zip archive stored in sidecar."""
    path = input_data.get("path", "").strip().lower()

    # Validate path
    if not path or len(path) > MAX_PATH_LENGTH:
        return {"error": "invalid_path", "message": f"Path must be 1-{MAX_PATH_LENGTH} chars"}
    if ".." in path:
        return {"error": "path_traversal", "message": "Path must not contain '..'"}
    if not PATH_PATTERN.match(path):
        return {"error": "invalid_path",
                "message": "Path must be lowercase alphanumeric + hyphens + slashes"}

    # Check deployment limit
    max_deployments = _static_config.get("max_deployments", MAX_DEPLOYMENTS)
    with _deployments_lock:
        if path not in _deployments and len(_deployments) >= max_deployments:
            return {"error": "limit_exceeded",
                    "message": f"Maximum {max_deployments} deployments reached"}

    # Get archive from sidecar
    archive_ref = input_data.get("archive", "")
    if not archive_ref:
        return {"error": "missing_archive", "message": "archive field required (asset hash)"}

    # Strip knarr-asset:// prefix if present
    archive_hash = archive_ref.replace("knarr-asset://", "")
    if not re.match(r'^[a-f0-9]{64}$', archive_hash):
        return {"error": "invalid_archive", "message": "archive must be a 64-char hex hash"}

    try:
        archive_data = _node.get_asset(archive_hash)
    except Exception as e:
        return {"error": "archive_not_found", "message": f"Cannot fetch archive: {e}"}

    # Validate zip
    try:
        return _extract_and_deploy(path, archive_data, archive_hash)
    except Exception as e:
        logger.error(f"knarr-static deploy failed: {e}")
        return {"error": "deploy_failed", "message": str(e)}


def _extract_and_deploy(path: str, archive_data: bytes, archive_hash: str) -> dict:
    """Validate zip archive, extract to temp, then atomic-move to static root."""
    max_extracted = _static_config.get("max_extracted_size", MAX_EXTRACTED_SIZE)

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, "archive.zip")
        with open(zip_path, "wb") as f:
            f.write(archive_data)

        try:
            zf = zipfile.ZipFile(zip_path, "r")
        except zipfile.BadZipFile:
            return {"error": "bad_archive", "message": "Not a valid zip file"}

        with zf:
            # Check file count
            members = zf.namelist()
            if len(members) > MAX_FILES:
                return {"error": "too_many_files",
                        "message": f"Archive has {len(members)} files, max {MAX_FILES}"}

            # Check total extracted size and path traversal
            total_size = 0
            for info in zf.infolist():
                if info.file_size < 0:
                    return {"error": "invalid_entry", "message": f"Negative size: {info.filename}"}
                total_size += info.file_size
                if total_size > max_extracted:
                    return {"error": "archive_too_large",
                            "message": f"Extracted size exceeds {max_extracted // (1024*1024)} MB"}
                # Path traversal check
                resolved = os.path.normpath(os.path.join(tmpdir, "extract", info.filename))
                extract_root = os.path.normpath(os.path.join(tmpdir, "extract"))
                if not resolved.startswith(extract_root + os.sep) and resolved != extract_root:
                    return {"error": "path_traversal",
                            "message": f"Path traversal in archive: {info.filename}"}

            # Extract to temp dir
            extract_dir = os.path.join(tmpdir, "extract")
            zf.extractall(extract_dir)

        # Verify index.html exists
        if not os.path.isfile(os.path.join(extract_dir, "index.html")):
            return {"error": "missing_index", "message": "Archive must contain index.html at root"}

        # Atomic deploy: move to static root
        static_root = _get_static_root()
        static_root.mkdir(parents=True, exist_ok=True)
        target = static_root / path

        # Remove existing deployment if any
        if target.exists():
            shutil.rmtree(target)

        shutil.copytree(extract_dir, str(target))

    # Track deployment
    file_count = sum(1 for _ in Path(target).rglob("*") if _.is_file())
    with _deployments_lock:
        _deployments[path] = {
            "files": file_count,
            "deployed_at": os.path.getmtime(str(target)),
            "archive_hash": archive_hash,
        }

    logger.info(f"knarr-static: deployed '{path}' ({file_count} files)")
    return {
        "status": "deployed",
        "path": path,
        "url": f"/s/{path}/",
        "files": file_count,
    }


def _handle_undeploy(input_data: dict) -> dict:
    """Remove a deployed static site."""
    path = input_data.get("path", "").strip().lower()
    if not path:
        return {"error": "missing_path", "message": "path field required"}

    with _deployments_lock:
        if path not in _deployments:
            return {"error": "not_found", "message": f"No deployment at '{path}'"}

    static_root = _get_static_root()
    target = static_root / path

    # Path confinement check
    resolved = target.resolve()
    root_resolved = static_root.resolve()
    if not str(resolved).startswith(str(root_resolved) + os.sep):
        return {"error": "path_traversal", "message": "Invalid path"}

    if target.exists():
        shutil.rmtree(target)

    with _deployments_lock:
        _deployments.pop(path, None)
    logger.info(f"knarr-static: undeployed '{path}'")
    return {"status": "undeployed", "path": path}


def _handle_list() -> dict:
    """List all deployed static sites."""
    sites = []
    with _deployments_lock:
        items = sorted(_deployments.items())
    for path, info in items:
        sites.append({
            "path": path,
            "url": f"/s/{path}/",
            "files": info["files"],
            "deployed_at": info["deployed_at"],
            "archive_hash": info["archive_hash"],
        })
    return {"status": "ok", "sites": sites, "count": len(sites)}


def get_static_root() -> Path:
    """Public accessor for the static root directory."""
    return _get_static_root()


def is_static_deployment(path: str) -> bool:
    """Check if a path has a static deployment."""
    with _deployments_lock:
        return path.lower() in _deployments
