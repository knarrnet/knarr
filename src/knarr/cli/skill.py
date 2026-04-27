import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any
from .config import parse_skill_toml, remove_dynamic_skill, _cleanup_handler_module
from ..core.crypto import SigningKey, VerifyKey
from ..core.proof import sign_document, verify_document

EXCLUDE_PATTERNS = {"tests", "__pycache__", ".git", ".env", "data"}

def cmd_skill_init(name: str) -> str:
    """Scaffold a new skill package directory."""
    if not re.match(r'^[a-z0-9-]+$', name) or len(name) > 64:
        raise ValueError(f"Invalid skill name: {name}. Must be lowercase alphanumeric + hyphens, max 64 chars.")

    os.makedirs(name, exist_ok=False)  # fail if exists

    skill_toml = f'''[skill]
name = "{name}"
version = "0.1.0"
handler = "handler.py:handle"
description = ""
tags = []

[skill.schema]
input = {{text = "string"}}
output = {{result = "string"}}
'''

    handler_py = '''async def handle(input_data: dict) -> dict:
    """Skill handler. Modify this function to implement your skill."""
    return {"result": input_data.get("text", "")}
'''

    with open(os.path.join(name, "skill.toml"), "w") as f:
        f.write(skill_toml)
    with open(os.path.join(name, "handler.py"), "w") as f:
        f.write(handler_py)

    return f"""Created skill package in {name}/

  skill.toml   Skill manifest
  handler.py   Skill handler
"""


def _archive_sig_path(archive_path: str | os.PathLike) -> Path:
    path = Path(archive_path)
    return path.with_suffix(path.suffix + ".sig")


def _sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _node_id_for_public_key(public_key_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()


def _verify_archive_signature(archive_path: str | os.PathLike, verify_signer: str) -> dict:
    sig_path = _archive_sig_path(archive_path)
    if not sig_path.is_file():
        raise ValueError(f"Missing detached signature file: {sig_path}")
    try:
        signed = json.loads(sig_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid detached signature file: {exc}") from exc

    if signed.get("document_type") != "knarr_archive_signature":
        raise ValueError("Invalid archive signature document")
    expected_hash = _sha256_file(archive_path)
    signed_hash = str(signed.get("archive_sha256", "") or "")
    if signed_hash != expected_hash:
        raise ValueError("Archive hash mismatch")

    public_key_hex = str(signed.get("signer_public_key", "") or "")
    try:
        verify_key = VerifyKey(bytes.fromhex(public_key_hex))
        derived_node_id = _node_id_for_public_key(public_key_hex)
    except Exception as exc:
        raise ValueError("Invalid signer public key in detached signature") from exc

    signer = str(signed.get("signer", "") or "")
    if signer and signer != derived_node_id:
        raise ValueError("Signer node id does not match signer public key")
    proof_vm = str((signed.get("proof") or {}).get("verificationMethod", "") or "")
    if proof_vm != f"did:knarr:{derived_node_id}#key-1":
        raise ValueError("Archive signature verification method mismatch")
    if not verify_document(signed, verify_key):
        raise ValueError("Archive signature verification failed")

    wanted = str(verify_signer or "").strip()
    if wanted.lower() not in {"any", "*"}:
        accepted = {
            derived_node_id,
            public_key_hex,
            f"did:knarr:{derived_node_id}",
            f"did:knarr:{derived_node_id}#key-1",
        }
        if wanted.lower() not in accepted:
            raise ValueError("Archive signer mismatch")
    return signed


def cmd_skill_install(source: str, config_dir: str, force: bool = False, upgrade: bool = False,
                      parent_bundle: str = "", visited: Optional[set] = None,
                      verify_signer: Optional[str] = None,
                      policy: Optional[dict] = None) -> str:
    """Install a skill from a local directory, .knarr archive, or git URL.

    C-05 (v0.58.0): when verify_signer is provided, check detached .sig,
    verify archive sha256 matches, verify signer. Missing sig → refuse.
    Hash mismatch → refuse. Signer mismatch → refuse. Use "any" to accept
    any valid signer.
    """
    if visited is None:
        visited = set()

    # C-05: policy-based require_signed_packages enforcement
    _policy = policy or {}
    if _policy.get("require_signed_packages", False) and verify_signer is None:
        verify_signer = "any"

    # C-05: verify signature if requested
    if verify_signer is not None:
        _verify_archive_signature(source, verify_signer)

    # 1. Resolve source to a directory
    source_dir, cleanup_fn = _resolve_source(source)

    try:
        # 2. Parse and validate skill.toml
        toml_path = os.path.join(source_dir, "skill.toml")
        if not os.path.exists(toml_path):
            raise ValueError(f"No skill.toml found in {source_dir}")
        manifest = parse_skill_toml(toml_path)
        name = manifest["name"]
        version = manifest["version"]

        if name in visited:
            return f"Skill '{name}' already visited in this installation cycle, skipping"
        visited.add(name)

        # 3. Check for conflicts
        skills_dir = os.path.join(config_dir, "skills")
        target_dir = os.path.join(skills_dir, name)
        if os.path.exists(target_dir):
            if not force and not upgrade:
                # Check version
                existing_toml = os.path.join(target_dir, "skill.toml")
                if os.path.exists(existing_toml):
                    try:
                        existing = parse_skill_toml(existing_toml)
                        if existing["version"] == version:
                            return f"Skill '{name}' v{version} already installed (use --force to overwrite)"
                    except Exception:
                        pass

        # 4. Install bundled dependencies (if archive with deps/)
        deps_dir = os.path.join(source_dir, "deps")
        if os.path.isdir(deps_dir):
            for dep_file in sorted(os.listdir(deps_dir)):
                if dep_file.endswith(".knarr"):
                    dep_path = os.path.join(deps_dir, dep_file)
                    _install_bundled_dep(dep_path, config_dir, parent_bundle=name, visited=visited)

        # 5. Install Python dependencies (if any)
        _install_python_deps(source_dir, manifest)

        # 6. Copy to skills directory
        os.makedirs(target_dir, exist_ok=True)
        data_dir = os.path.join(target_dir, manifest.get("runtime", {}).get("data_dir", "data"))
        _copy_skill_files(source_dir, target_dir, upgrade=upgrade, data_dir=data_dir)

        # 7. Handle hot assets
        _install_hot_assets(source_dir, manifest, data_dir)

        # 8. Handle fetch assets
        _install_fetch_assets(manifest, data_dir)

        # 9. Write install metadata
        _write_install_metadata(target_dir, source, parent_bundle=parent_bundle)

        # 10. Update knarr.toml
        _update_knarr_toml(config_dir, manifest)

        # 11. Dependency and circular checks
        warnings = _check_dependencies(manifest, config_dir)
        circ_warning = _check_circular_deps(name, config_dir, current_manifest=manifest)
        if circ_warning:
            warnings.append(circ_warning)

        # 12. Signal hot-reload
        _signal_reload(config_dir)

        msg = f"Installed {name} v{version} from {source}"
        if warnings:
            msg += "\n\n" + "\n".join(warnings)
        return msg

    finally:
        if cleanup_fn:
            cleanup_fn()

def cmd_skill_remove(name: str, config_dir: str, purge: bool = False) -> str:
    """Remove an installed skill.

    C-01 (v0.58.0): also recognizes flat dynamic-skill file layout.
    Non-existent skill → no-op + log.
    """
    import logging
    _remove_log = logging.getLogger("knarr.cli.skill")

    # C-01: check for flat dynamic-skill layout first
    skills_path = os.path.join(config_dir, "knarr.skills.toml")
    is_dynamic = False
    dynamic_skill_cfg: dict = {}
    if os.path.isfile(skills_path):
        try:
            with open(skills_path, "rb") as f:
                import tomllib as _tt
                data = _tt.load(f)
            skills = data.get("skills", {})
            if name in skills:
                is_dynamic = True
                dynamic_skill_cfg = skills[name]
        except Exception:
            pass

    target_dir = os.path.join(config_dir, "skills", name)

    if not os.path.exists(target_dir) and not is_dynamic:
        _remove_log.info("Skill '%s' is not installed (no-op)", name)
        return f"Skill '{name}' is not installed"

    # C-01 (v0.58.0): handle flat dynamic-skill layout
    if is_dynamic:
        from pathlib import Path as _Path
        ok = remove_dynamic_skill(_Path(config_dir), name)
        if not ok:
            return f"Failed to remove dynamic skill '{name}'"

        # Remove the handler file if it exists and is within config_dir
        handler = dynamic_skill_cfg.get("handler", "")
        if handler and ":" in handler:
            handler_file = handler.split(":")[0]
            if not os.path.isabs(handler_file):
                handler_file = os.path.join(config_dir, handler_file)
            try:
                resolved = Path(handler_file).resolve()
                config_root = Path(config_dir).resolve()
                confined = resolved.is_relative_to(config_root)
            except Exception:
                confined = False
            if confined and os.path.isfile(handler_file):
                try:
                    os.remove(handler_file)
                except OSError as exc:
                    _remove_log.warning(f"Failed to remove handler file {handler_file}: {exc}")

        _signal_reload(config_dir)
        _remove_log.info("Removed dynamic skill: %s", name)
        return f"Removed dynamic skill '{name}'"

    # Read manifest to find data_dir
    data_dir_name = "data"
    toml_path = os.path.join(target_dir, "skill.toml")
    if os.path.exists(toml_path):
        try:
            with open(toml_path, "rb") as f:
                manifest = tomllib.load(f)
            data_dir_name = manifest.get("runtime", {}).get("data_dir", "data")
        except Exception:
            pass

    # Remove from knarr.toml
    _remove_from_knarr_toml(config_dir, name)

    # Delete files
    if purge:
        shutil.rmtree(target_dir)
    else:
        # Preserve data_dir
        data_dir = os.path.join(target_dir, data_dir_name)
        has_data = os.path.isdir(data_dir)
        if has_data:
            # Move data_dir out, delete everything, move data_dir back
            tmp_data = target_dir + "_data_backup"
            if os.path.exists(tmp_data):
                shutil.rmtree(tmp_data)
            shutil.move(data_dir, tmp_data)
            shutil.rmtree(target_dir)
            os.makedirs(target_dir, exist_ok=True)
            shutil.move(tmp_data, data_dir)
        else:
            shutil.rmtree(target_dir)

    # Signal reload — node detects missing skill and calls deregister()
    _signal_reload(config_dir)

    msg = f"Removed skill '{name}'"
    if not purge:
        msg += " (data directory preserved, use --purge to delete)"
    return msg

def cmd_skill_list(config_dir: str, json_output: bool = False) -> str:
    """List installed skills."""
    toml_path = os.path.join(config_dir, "knarr.toml")
    if not os.path.exists(toml_path):
        return "No knarr.toml found"

    with open(toml_path, "rb") as f:
        config = tomllib.load(f)

    skills = config.get("skills", {})
    if not skills:
        return "No skills installed"

    rows = []
    for name, skill_cfg in sorted(skills.items()):
        version = skill_cfg.get("version", "?")
        visibility = skill_cfg.get("visibility", "public")

        # Read install source from installed skill.toml
        installed_toml = os.path.join(config_dir, "skills", name, "skill.toml")
        source = "local"
        if os.path.exists(installed_toml):
            try:
                with open(installed_toml, "rb") as f:
                    installed = tomllib.load(f)
                source = installed.get("install", {}).get("source", "local")
            except Exception:
                pass

        rows.append((name, version, visibility, source))

    if json_output:
        import json
        return json.dumps([{"name": n, "version": v, "visibility": vis, "source": s} for n, v, vis, s in rows], indent=2)

    # Table format
    header = f"{'NAME':<20} {'VERSION':<10} {'VISIBILITY':<12} {'SOURCE'}"
    lines = [header]
    for name, version, visibility, source in rows:
        lines.append(f"{name:<20} {version:<10} {visibility:<12} {source}")
    return "\n".join(lines)

def _resolve_source(source: str) -> tuple:
    """Resolve install source to (directory_path, cleanup_function_or_None)."""
    if source.startswith("git+"):
        # Git clone to temp dir
        url = source[4:]  # strip "git+" prefix
        tmp = tempfile.mkdtemp(prefix="knarr_install_")
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", url, tmp],
                check=True, capture_output=True, text=True
            )
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ValueError(f"Failed to clone from {url}: {e}")
        return tmp, lambda: shutil.rmtree(tmp, ignore_errors=True)

    elif source.endswith(".knarr"):
        # Extract zip archive to temp dir
        tmp = tempfile.mkdtemp(prefix="knarr_install_")
        try:
            with zipfile.ZipFile(source, "r") as zf:
                # Validate: must have a single root directory and no Zip-slip
                names = zf.namelist()
                root_dirs = set()
                for name in names:
                    if name.startswith("/") or ".." in name or "\\" in name:
                        raise ValueError(f"Invalid .knarr archive: malicious entry name detected: {name}")
                    if "/" in name:
                        root_dirs.add(name.split("/")[0])
                
                if len(root_dirs) != 1:
                    raise ValueError(f"Invalid .knarr archive: expected single root directory, found {root_dirs}")
                zf.extractall(tmp)
                root = os.path.join(tmp, root_dirs.pop())
            return root, lambda: shutil.rmtree(tmp, ignore_errors=True)
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            raise e

    else:
        # Local directory
        source = os.path.abspath(source)
        if not os.path.isdir(source):
            raise ValueError(f"Not a directory: {source}")
        return source, None

def _copy_skill_files(source_dir: str, target_dir: str, upgrade: bool, data_dir: str):
    """Copy skill files, excluding tests/__pycache__/.git/.env/data."""
    for item in os.listdir(source_dir):
        if item in EXCLUDE_PATTERNS:
            continue
        src = os.path.join(source_dir, item)
        dst = os.path.join(target_dir, item)
        if os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*EXCLUDE_PATTERNS))
        else:
            shutil.copy2(src, dst)

def _update_knarr_toml(config_dir: str, manifest: dict):
    """Add or update [skills.<name>] section in knarr.toml."""
    toml_path = os.path.join(config_dir, "knarr.toml")

    # Read existing config
    config = {}
    if os.path.exists(toml_path):
        with open(toml_path, "rb") as f:
            config = tomllib.load(f)

    name = manifest["name"]
    handler_spec = f"skills/{name}/{manifest['handler']}"

    # Build skill config entry
    skill_entry = {
        "handler": handler_spec,
        "version": manifest["version"],
        "description": manifest.get("description", ""),
        "tags": manifest.get("tags", []),
    }

    # Map schema fields
    schema = manifest.get("schema", {})
    if schema.get("input"):
        skill_entry["input_schema"] = schema["input"]
    if schema.get("output"):
        skill_entry["output_schema"] = schema["output"]

    # Map pricing fields
    pricing = manifest.get("pricing", {})
    if "price" in pricing:
        skill_entry["price"] = pricing["price"]
    if "max_input_size" in pricing:
        skill_entry["max_input_size"] = pricing["max_input_size"]

    # Map visibility
    vis = manifest.get("visibility", {})
    if "default" in vis:
        skill_entry["visibility"] = vis["default"]

    # Merge — preserve manual overrides
    if "skills" not in config:
        config["skills"] = {}
    existing = config["skills"].get(name, {})
    existing.update(skill_entry)
    config["skills"][name] = existing

    # Write back
    _write_knarr_toml(toml_path, config)

def _remove_from_knarr_toml(config_dir: str, name: str):
    """Remove [skills.<name>] section from knarr.toml."""
    toml_path = os.path.join(config_dir, "knarr.toml")
    if not os.path.exists(toml_path):
        return

    with open(toml_path, "rb") as f:
        config = tomllib.load(f)

    if "skills" in config and name in config["skills"]:
        del config["skills"][name]
        _write_knarr_toml(toml_path, config)

def _write_knarr_toml(path: str, config: dict):
    """Robust recursive TOML serializer with atomic write."""
    lines = []
    
    # 1. Non-table keys at root FIRST
    for k, v in sorted(config.items()):
        if not isinstance(v, dict):
            lines.append(f"{_toml_key(k)} = {_toml_val(v)}")
    
    if lines:
        lines.append("")

    # 2. Known sections in preferred order
    known_sections = ["node", "network", "bridges", "policy", "skill", "requirements", "dependencies", "runtime", "bundle", "install", "sidecar"]
    
    # Track what we've written
    written_sections = set()

    for section in known_sections:
        if section in config and isinstance(config[section], dict):
            lines.append(f"[{section}]")
            for k, v in sorted(config[section].items()):
                lines.append(f"{_toml_key(k)} = {_toml_val(v)}")
            lines.append("")
            written_sections.add(section)

    # 3. Special handling for [skills.*] (nested tables)
    if "skills" in config and isinstance(config["skills"], dict):
        for skill_name, skill_cfg in sorted(config["skills"].items()):
            lines.append(f"[skills.{_toml_key(skill_name)}]")
            for k, v in sorted(skill_cfg.items()):
                lines.append(f"{_toml_key(k)} = {_toml_val(v)}")
            lines.append("")
        written_sections.add("skills")

    # 4. Any remaining unknown sections
    for section, content in sorted(config.items()):
        if section not in written_sections and isinstance(content, dict):
            lines.append(f"[{section}]")
            for k, v in sorted(content.items()):
                lines.append(f"{_toml_key(k)} = {_toml_val(v)}")
            lines.append("")

    # Atomic write
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write("\n".join(lines).strip() + "\n")
    os.replace(tmp_path, path)

def _is_inline_table(d: dict) -> bool:
    """True if dict should be serialized as an inline table {a=1}."""
    # We use inline tables for schemas and pricing overrides
    return any(k in ["input_schema", "output_schema", "pricing", "input", "output", "assets"] for k in d.keys()) or len(d) <= 2

def _toml_key(k: str) -> str:
    """Quote key if it contains special characters."""
    if re.match(r'^[A-Za-z0-9_-]+$', k):
        return k
    _escaped = k.replace("\\", "\\\\").replace("\"", "\\\"")
    return f'"{_escaped}"'

def _toml_val(v):
    if isinstance(v, str):
        # Escape quotes and backslashes
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f'{_toml_key(k)} = {_toml_val(v2)}' for k, v2 in sorted(v.items())) + "}"
    return str(v)

def _write_install_metadata(target_dir: str, source: str, parent_bundle: str):
    """Update [install] section in the installed skill.toml."""
    toml_path = os.path.join(target_dir, "skill.toml")
    
    # Read existing
    with open(toml_path, "rb") as f:
        config = tomllib.load(f)
    
    # Update install section
    config["install"] = {
        "source": source,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "installed_from_bundle": parent_bundle or ""
    }
    
    # Write back using our robust serializer
    _write_knarr_toml(toml_path, config)

def _signal_reload(config_dir: str):
    """Signal running node to reload config. Uses SIGHUP on Unix, sentinel file everywhere."""
    # Always write sentinel file (cross-platform, works on Windows and WSL2)
    sentinel = os.path.join(config_dir, "knarr.reload")
    try:
        with open(sentinel, "w") as f:
            f.write("")
    except OSError:
        pass

    # Also send SIGHUP on Unix for immediate reload (sentinel has up to 2s delay)
    if os.name != "nt" and hasattr(signal, "SIGHUP"):
        pid_path = os.path.join(config_dir, "knarr.pid")
        if os.path.exists(pid_path):
            try:
                with open(pid_path) as f:
                    pid = int(f.read().strip())
                os.kill(pid, signal.SIGHUP)
            except (ValueError, ProcessLookupError, PermissionError):
                pass

def _install_python_deps(source_dir: str, manifest: dict):
    """Install Python dependencies (informational warning on failure)."""
    req_file = os.path.join(source_dir, "requirements.txt")
    packages = manifest.get("requirements", {}).get("packages", [])
    
    if os.path.exists(req_file):
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_file],
                           check=False, capture_output=True)
        except Exception:
            pass
            
    if packages:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install"] + packages,
                           check=False, capture_output=True)
        except Exception:
            pass

def _install_bundled_dep(dep_path: str, config_dir: str, parent_bundle: str, visited: set):
    """Recursive install for bundled dependencies."""
    cmd_skill_install(dep_path, config_dir, force=False, upgrade=True, parent_bundle=parent_bundle, visited=visited)

def _install_hot_assets(source_dir: str, manifest: dict, data_dir: str):
    """Extract hot assets to data_dir."""
    hot = manifest.get("assets", {}).get("hot", {})
    if not hot:
        return
    
    os.makedirs(data_dir, exist_ok=True)
    for name, cfg in hot.items():
        rel_path = cfg.get("path")
        if not rel_path: continue
        src = os.path.join(source_dir, rel_path)
        if os.path.exists(src):
            dst = os.path.join(data_dir, os.path.basename(rel_path))
            shutil.copy2(src, dst)

def _check_dependencies(manifest: dict, config_dir: str) -> List[str]:
    """Check skill dependencies. Returns list of warning strings."""
    warnings = []
    deps = manifest.get("dependencies", {})

    for uri, dep_cfg in deps.items():
        dep_name = _extract_name_from_uri(uri)
        required = dep_cfg.get("required", True) if isinstance(dep_cfg, dict) else True

        # Check if installed locally
        installed = os.path.isdir(os.path.join(config_dir, "skills", dep_name)) if dep_name else False

        if not installed:
            if required:
                warnings.append(f"WARNING: Required dependency not found: {uri}")
            else:
                warnings.append(f"NOTE: Optional dependency not available: {uri}")

    return warnings

def _check_circular_deps(name: str, config_dir: str, current_manifest: Optional[dict] = None) -> Optional[str]:
    """Check for circular dependencies. Returns warning string if cycle found, None otherwise."""
    # Build dependency graph from all installed skills
    graph = {}  # skill_name -> set of dependency names
    
    # Include currently installing skill
    if current_manifest:
        dep_names = set()
        for uri in current_manifest.get("dependencies", {}):
            d_name = _extract_name_from_uri(uri)
            if d_name:
                dep_names.add(d_name)
        graph[current_manifest["name"]] = dep_names

    skills_dir = os.path.join(config_dir, "skills")
    if os.path.isdir(skills_dir):
        for skill_name in os.listdir(skills_dir):
            toml_path = os.path.join(skills_dir, skill_name, "skill.toml")
            if os.path.exists(toml_path):
                try:
                    m = parse_skill_toml(toml_path)
                    s_name = m.get("name", skill_name)
                    
                    if s_name in graph: continue # already added if current
                    
                    deps_section = m.get("dependencies", {})
                    dep_names = set()
                    for uri in deps_section:
                        d_name = _extract_name_from_uri(uri)
                        if d_name:
                            dep_names.add(d_name)
                    graph[s_name] = dep_names
                except Exception:
                    continue

    # DFS cycle detection
    visited_nodes = set()
    path = set()

    def has_cycle(node):
        if node in path:
            return True
        if node in visited_nodes:
            return False
        visited_nodes.add(node)
        path.add(node)
        for neighbor in graph.get(node, set()):
            if has_cycle(neighbor):
                return True
        path.remove(node)
        return False

    # Check for cycles starting from ANY node in the graph
    for start_node in sorted(graph.keys()):
        # Reset path for each start node
        path = set()
        if has_cycle(start_node):
            return f"Warning: Circular dependency detected involving '{start_node}'. This is allowed for network dependencies but may indicate a configuration issue."
    
    return None

def _install_fetch_assets(manifest: dict, data_dir: str):
    """Download fetch assets to data_dir (mocked in 8b)."""
    fetch = manifest.get("assets", {}).get("fetch", {})
    if not fetch:
        return
    # Not implemented fully in 8b (requires HTTP client), just ensure data_dir exists
    os.makedirs(data_dir, exist_ok=True)

def cmd_skill_pack(directory: str, sign: bool = False, node=None) -> str:
    """Create a .knarr archive from a skill directory.

    C-05 (v0.58.0): when sign=True, sign the archive sha256 using sign_document
    from core/proof.py (eddsa-jcs-2022). Write detached .sig alongside the
    archive. Requires live node; refuse with clear error if unreachable.
    """
    # C-05: check node availability before any I/O
    if sign and node is None:
        raise ValueError("--sign requires a live node (use 'knarr skill pack --sign' from node CLI)")
    if sign:
        signing_key = getattr(node, "_signing_key", None)
        if signing_key is None:
            raise ValueError("Node signing key unavailable — cannot sign archive")

    directory = os.path.abspath(directory)
    toml_path = os.path.join(directory, "skill.toml")
    if not os.path.exists(toml_path):
        raise ValueError(f"No skill.toml found in {directory}")

    manifest = parse_skill_toml(toml_path)
    name = manifest["name"]
    version = manifest["version"]
    archive_name = f"{name}-{version}.knarr"
    root_prefix = f"{name}-{version}/"

    with zipfile.ZipFile(archive_name, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(directory):
            # Filter excluded directories
            dirs[:] = [d for d in dirs if d not in EXCLUDE_PATTERNS]
            for file in files:
                if file.startswith("."):
                    continue
                abs_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_path, directory)
                zf.write(abs_path, root_prefix + rel_path)

    result = f"Created {archive_name}"

    # C-05: sign the archive if requested
    if sign:
        # Read archive sha256
        with open(archive_name, "rb") as f:
            archive_hash = hashlib.sha256(f.read()).hexdigest()

        # Sign using node's signing key (#key-1 convention)
        from knarr.core.proof import sign_document

        node_id = node.node_info.node_id if hasattr(node, "node_info") else "unknown"
        verification_method = f"did:knarr:{node_id}#key-1"

        signer_pubkey_hex = signing_key.verify_key.encode().hex()
        sig_doc = sign_document(
            document={
                "document_type": "knarr_archive_signature",
                "archive": archive_name,
                "archive_sha256": archive_hash,
                "skill_name": name,
                "skill_version": version,
                "signer": node_id,
                "signer_public_key": signer_pubkey_hex,
            },
            private_key=signing_key,
            verification_method=verification_method,
        )

        # Write detached .sig
        import json as _json
        sig_path = f"{archive_name}.sig"
        with open(sig_path, "w") as f:
            _json.dump(sig_doc, f, indent=2, sort_keys=True)

        result += f"\nSigned → {sig_path} (sha256: {archive_hash[:16]}...)"

    return result

def cmd_skill_export(name: str, config_dir: str, bundle: bool = False) -> str:
    """Export an installed skill as a .knarr archive."""
    skill_dir = os.path.join(config_dir, "skills", name)
    if not os.path.isdir(skill_dir):
        raise ValueError(f"Skill '{name}' is not installed")

    toml_path = os.path.join(skill_dir, "skill.toml")
    manifest = parse_skill_toml(toml_path)
    version = manifest["version"]
    archive_name = f"{name}-{version}.knarr"
    root_prefix = f"{name}-{version}/"

    with zipfile.ZipFile(archive_name, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add skill files
        for root, dirs, files in os.walk(skill_dir):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_PATTERNS]
            for file in files:
                abs_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_path, skill_dir)
                zf.write(abs_path, root_prefix + rel_path)

        # Bundle dependencies if requested
        if bundle:
            deps = manifest.get("dependencies", {})
            bundled = []
            for dep_uri in deps:
                dep_name = _extract_name_from_uri(dep_uri)
                dep_path = os.path.join(config_dir, "skills", dep_name) if dep_name else None
                if dep_path and os.path.isdir(dep_path):
                    # Pack the dependency
                    dep_archive_data = _pack_skill_to_bytes(dep_path)
                    dep_manifest = parse_skill_toml(os.path.join(dep_path, "skill.toml"))
                    dep_archive_name = f"{dep_name}-{dep_manifest['version']}.knarr"
                    zf.writestr(root_prefix + "deps/" + dep_archive_name, dep_archive_data)
                    bundled.append(f"{dep_name}-{dep_manifest['version']}")
                else:
                    print(f"Warning: dependency '{dep_uri}' not installed locally, skipping")

            if bundled:
                return f"Bundled: {', '.join(bundled)}\nCreated {archive_name}"

    return f"Created {archive_name}"

def _extract_name_from_uri(uri: str) -> Optional[str]:
    """Extract skill name from a knarr:/// URI."""
    # Strip scheme and authority
    path = uri.split("///", 1)[-1] if "///" in uri else uri
    # Strip version
    path = path.split("@")[0] if "@" in path else path
    # Last path component is the name
    return path.rsplit("/", 1)[-1] if "/" in path else path

def _pack_skill_to_bytes(directory: str) -> bytes:
    """Pack a skill directory into a .knarr zip in memory."""
    import io
    toml_path = os.path.join(directory, "skill.toml")
    manifest = parse_skill_toml(toml_path)
    name = manifest["name"]
    version = manifest["version"]
    root_prefix = f"{name}-{version}/"
    
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_PATTERNS]
            for file in files:
                if file.startswith("."): continue
                abs_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_path, directory)
                zf.write(abs_path, root_prefix + rel_path)
    return buf.getvalue()
