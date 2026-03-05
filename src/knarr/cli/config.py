import importlib.util
import os
import re
import socket
import sys
import tomllib
from pathlib import Path
from typing import Dict, Any, Optional

DEFAULT_CONFIG = {
    "node": {"port": 9000, "host": "0.0.0.0", "storage": "node.db", "task_slots": 4},
    "network": {"bootstrap": []},
    "skills": {},
    "bridges": {},
    "policy": {"initial_credit": 3.0, "min_balance": -10.0, "tit_for_tat": False},
    "mail": {"accept_from": "all", "default_ttl_hours": 72, "max_messages": 10000, "whitelist": [], "price": 1.0},
    "cockpit": {"port": 0, "bind": "127.0.0.1", "auth_token": ""},
}

def merge_defaults(defaults: dict, overrides: dict) -> dict:
    """Deep-merge overrides into defaults."""
    result = {}
    for key in set(defaults) | set(overrides):
        if key in overrides and key in defaults:
            if isinstance(defaults[key], dict) and isinstance(overrides[key], dict):
                result[key] = merge_defaults(defaults[key], overrides[key])
            else:
                result[key] = overrides[key]
        elif key in overrides:
            result[key] = overrides[key]
        else:
            result[key] = defaults[key]
    return result

def load_config(path: Path, explicit: bool = False) -> dict:
    """Load and validate knarr.toml + knarr.skills.toml. Returns merged config."""
    if not path.exists():
        if explicit:
            print(f"Error: Config file not found: {path}", file=sys.stderr)
            print(f"  Check the path passed to --config.", file=sys.stderr)
            sys.exit(1)
        return DEFAULT_CONFIG

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        _warn_unknown_keys(raw, path)
        config = merge_defaults(DEFAULT_CONFIG, raw)
    except tomllib.TOMLDecodeError as e:
        print(f"Error: Invalid TOML in {path}: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error in {path}: {e}", file=sys.stderr)
        print("Check the configuration reference in the README.", file=sys.stderr)
        sys.exit(1)

    # v0.37.0: Merge dynamic skills from knarr.skills.toml
    config_dir = path.parent
    dynamic_skills = load_dynamic_skills(config_dir)
    if dynamic_skills:
        existing = config.get("skills", {})
        # Dynamic skills never overwrite static skills
        for name, cfg in dynamic_skills.items():
            if name not in existing:
                existing[name] = cfg
        config["skills"] = existing

    return config

_KNOWN_KEYS = {
    "node": {"port", "host", "storage", "advertise_host", "sidecar_port", "max_asset_size",
             "max_task_timeout", "task_slots", "min_protocol_version",
             "auto_upgrade", "backup_retention_days", "wallet", "jurisdiction",
             "event_bus_size", "event_bus_debug", "max_queue_depth",
             "log_retention", "log_retention_hours", "housekeeping_retention_days"},
    "economy": {"default_soft_limit", "default_hard_limit"},
    "skills": {"minimum_price", "default_timeout"},
    "settlement": {"tab_reminder_threshold", "netting_interval"},
    "network": {"bootstrap", "upnp", "tls_cert", "tls_key", "max_connections", "connection_idle_timeout", "gossip_fanout", "heartbeat_silence_threshold", "peer_dead_timeout", "min_peers"},
    "sidecar": {"asset_dir"},
    "policy": {"initial_credit", "min_balance", "tit_for_tat", "group", "skill",
               "max_dynamic_skills", "dynamic_price_floor", "dynamic_price_ceiling",
               "dynamic_ttl_hours", "dynamic_allowed_handlers", "dynamic_enabled"},
    "mail": {"accept_from", "default_ttl_hours", "max_messages", "whitelist", "price", "debug", "stale_inbox_hours", "max_inbox", "pull_interval", "max_pull_batch", "accept_groups"},
    "cockpit": {"port", "bind", "auth_token", "tls", "tls_cert", "tls_key", "allowed_ips"},
    "token": {"mint", "rpc_url"},
    "static": {"enabled", "max_deployments", "max_extracted_size"},
    # peer_overrides is a free-form section (node_id -> "host:port"), not validated per-key
}

def _warn_unknown_keys(raw: dict, path: Path):
    """Warn about unrecognized keys in known sections to catch typos."""
    for section, known in _KNOWN_KEYS.items():
        if section in raw and isinstance(raw[section], dict):
            for key in raw[section]:
                if key not in known:
                    print(f"Warning: Unknown key '{key}' in [{section}] in {path}", file=sys.stderr)

def load_handler(handler_spec: str, config_dir: str, skill_name: Optional[str] = None) -> Any:
    """Load a handler function from a file path spec."""
    if ":" in handler_spec:
        file_path, func_name = handler_spec.rsplit(":", 1)
    else:
        file_path = handler_spec
        func_name = "handle"

    # Resolve relative to config directory
    if not os.path.isabs(file_path):
        file_path = os.path.join(config_dir, file_path)
    file_path = os.path.abspath(file_path)

    # SA-09: Ensure handler path stays within config directory
    config_dir_abs = os.path.abspath(config_dir)
    if not file_path.startswith(config_dir_abs + os.sep) and file_path != config_dir_abs:
        raise ImportError(
            f"Handler path escapes config directory: {file_path}\n"
            f"  Handlers must be located within: {config_dir_abs}"
        )

    if not os.path.exists(file_path):
        raise FileNotFoundError(
            f"Handler file not found: {file_path}\n"
            f"  Check the 'handler' path in your knarr.toml"
        )

    if skill_name:
        module_name = f"knarr_skill_{skill_name}"
    else:
        module_name = f"knarr_skill_{os.path.basename(file_path).replace('.py', '')}"
    
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(
            f"Cannot load Python module from: {file_path}\n"
            f"  Is it a valid .py file?"
        )

    module = importlib.util.module_from_spec(spec)
    # Register sys.modules before exec_module
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise ImportError(f"Error executing handler module {file_path}: {e}")

    handler = getattr(module, func_name, None)
    if handler is None:
        raise ImportError(
            f"No function '{func_name}' found in {file_path}\n"
            f"  Your handler file should define: async def {func_name}(input_data: dict) -> dict:"
        )

    return handler

def detect_advertise_host() -> str:
    """Detect the best advertise IP. Tries LAN first, falls back to public IP lookup."""
    lan_ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
        if not is_private_ip(lan_ip):
            return lan_ip
    except Exception:
        pass
    # LAN IP is private — try public IP lookup
    return _detect_public_ip() or lan_ip

def _detect_public_ip() -> str:
    """Query external service for public IP. Returns empty string on failure."""
    import urllib.request
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                candidate = resp.read().decode().strip()
                if candidate and not is_private_ip(candidate):
                    return candidate
        except Exception:
            continue
    return ""

def is_private_ip(ip: str) -> bool:
    """Check if an IP address is in a private range."""
    parts = ip.split(".")
    if len(parts) != 4:
        return True
    try:
        first, second = int(parts[0]), int(parts[1])
        return (first == 10 or
                (first == 172 and 16 <= second <= 31) or
                (first == 192 and second == 168) or
                first == 127)
    except (ValueError, IndexError):
        return True

def parse_skill_toml(path: str) -> dict:
    """Parse and validate a skill.toml manifest. Returns normalized dict.
    Raises ValueError on invalid manifest."""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"skill.toml: Invalid TOML format: {e}")

    skill = raw.get("skill", {})

    # Required fields
    name = skill.get("name", "")
    if not name:
        raise ValueError("skill.toml: [skill].name is required")
    if not re.match(r'^[a-z0-9-]+$', name) or len(name) > 64:
        raise ValueError(f"skill.toml: name must be lowercase alphanumeric + hyphens, max 64 chars: {name}")

    version = skill.get("version", "")
    if not version:
        raise ValueError("skill.toml: [skill].version is required")

    handler = skill.get("handler", "")
    if not handler:
        raise ValueError("skill.toml: [skill].handler is required")

    return {
        "name": name,
        "version": version,
        "handler": handler,
        "description": skill.get("description", ""),
        "tags": skill.get("tags", []),
        "license": skill.get("license", ""),
        "schema": skill.get("schema", {}),
        "pricing": skill.get("pricing", {}),
        "visibility": skill.get("visibility", {}),
        "assets": skill.get("assets", {}),
        "runtime": skill.get("runtime", {}),
        "requirements": raw.get("requirements", {}),
        "dependencies": raw.get("dependencies", {}),
        "bundle": raw.get("bundle", {}),
    }

def _cleanup_handler_module(skill_name: str):
    """Remove cached handler module from sys.modules so it can be reimported."""
    to_remove = [key for key in sys.modules if key.startswith(f"knarr_skill_{skill_name}")]
    for key in to_remove:
        del sys.modules[key]


# ── v0.37.0: Dynamic Skill Registration ────────────────────────────

import logging as _logging
import math as _math
import time as _time

_dyn_log = _logging.getLogger("knarr.dynamic_skills")

_DYNAMIC_DEFAULTS = {
    "max_dynamic_skills": 10,
    "dynamic_price_floor": 0.5,
    "dynamic_price_ceiling": 50.0,
    "dynamic_ttl_hours": 24,
    "dynamic_allowed_handlers": ["dynamic_facade.py"],
    "dynamic_enabled": False,
}


def _toml_escape(s: str) -> str:
    """Escape a string for safe inclusion in a TOML basic string (double-quoted)."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def _safe_toml_key(s: str) -> bool:
    """Check that a string is safe to use as a TOML bare key or section name."""
    return bool(re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$', s))


def _serialize_skills_toml(skills: dict) -> str:
    """Serialize skills dict to TOML string with proper escaping."""
    lines = []
    for sname, scfg in skills.items():
        if not _safe_toml_key(sname):
            continue  # Skip skill with unsafe name
        lines.append(f"[skills.{sname}]")
        for k, v in scfg.items():
            if not _safe_toml_key(k):
                continue  # Skip key with unsafe name
            if isinstance(v, bool):
                lines.append(f'{k} = {"true" if v else "false"}')
            elif isinstance(v, str):
                lines.append(f'{k} = "{_toml_escape(v)}"')
            elif isinstance(v, list):
                items = ", ".join(
                    f'"{_toml_escape(x)}"' if isinstance(x, str) else str(x)
                    for x in v
                )
                lines.append(f"{k} = [{items}]")
            else:
                lines.append(f"{k} = {v}")
        lines.append("")
    return "\n".join(lines)


def load_dynamic_skills(config_dir: Path) -> dict:
    """Load knarr.skills.toml and return its skills section (or empty dict)."""
    skills_path = config_dir / "knarr.skills.toml"
    if not skills_path.is_file():
        return {}
    try:
        with open(skills_path, "rb") as f:
            raw = tomllib.load(f)
        return raw.get("skills", {})
    except Exception as exc:
        _dyn_log.warning(f"Failed to load knarr.skills.toml: {exc}")
        return {}


def get_dynamic_policy(config: dict) -> dict:
    """Extract dynamic skill policy from config, with defaults."""
    policy = dict(_DYNAMIC_DEFAULTS)
    for key in _DYNAMIC_DEFAULTS:
        val = config.get("policy", {}).get(key)
        if val is not None:
            policy[key] = val
    return policy


def validate_dynamic_skill(
    skill_name: str, skill_cfg: dict, policy: dict, existing_count: int,
) -> tuple[bool, str]:
    """Validate a dynamic skill config against operator guardrails.

    Returns (ok, reason). Reason is empty string on success.
    """
    if not policy.get("dynamic_enabled", False):
        return False, "dynamic skills disabled by operator"

    if existing_count >= policy.get("max_dynamic_skills", 10):
        return False, f"max dynamic skills reached ({existing_count})"

    price = skill_cfg.get("price", 0)
    if not isinstance(price, (int, float)) or not _math.isfinite(price):
        return False, f"invalid price: {price!r}"

    floor = policy.get("dynamic_price_floor", 0.5)
    ceiling = policy.get("dynamic_price_ceiling", 50.0)
    if price < floor:
        return False, f"price {price} below floor {floor}"
    if price > ceiling:
        return False, f"price {price} above ceiling {ceiling}"

    handler = skill_cfg.get("handler", "")
    if ":" in handler:
        handler_file = handler.split(":")[0]
    else:
        handler_file = handler
    handler_basename = os.path.basename(handler_file)

    allowed = policy.get("dynamic_allowed_handlers", ["dynamic_facade.py"])
    if handler_basename not in allowed:
        return False, f"handler {handler_basename!r} not in allowed list"

    if not re.match(r'^[a-z0-9][a-z0-9-]*$', skill_name) or len(skill_name) > 64:
        return False, f"invalid skill name: {skill_name!r}"

    return True, ""


def write_dynamic_skill(config_dir: Path, skill_name: str, skill_cfg: dict) -> bool:
    """Write or update a single skill entry in knarr.skills.toml.

    Returns True on success. Touches knarr.reload sentinel to trigger reload.
    """
    skills_path = config_dir / "knarr.skills.toml"

    # Load existing
    existing = {}
    if skills_path.is_file():
        try:
            with open(skills_path, "rb") as f:
                existing = tomllib.load(f)
        except Exception:
            pass

    skills = existing.get("skills", {})
    skill_cfg["dynamic"] = True
    skill_cfg["created_at"] = skill_cfg.get("created_at", _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()))
    skills[skill_name] = skill_cfg

    # Write back as TOML
    try:
        skills_path.write_text(_serialize_skills_toml(skills), encoding="utf-8")
    except Exception as exc:
        _dyn_log.error(f"Failed to write knarr.skills.toml: {exc}")
        return False

    # Touch sentinel to trigger reload
    sentinel = config_dir / "knarr.reload"
    try:
        sentinel.touch()
    except Exception:
        pass

    _dyn_log.info(f"Registered dynamic skill: {skill_name}")
    return True


def remove_dynamic_skill(config_dir: Path, skill_name: str) -> bool:
    """Remove a dynamic skill from knarr.skills.toml and touch sentinel."""
    skills_path = config_dir / "knarr.skills.toml"
    if not skills_path.is_file():
        return False

    try:
        with open(skills_path, "rb") as f:
            existing = tomllib.load(f)
    except Exception:
        return False

    skills = existing.get("skills", {})
    if skill_name not in skills:
        return False

    del skills[skill_name]

    # Rewrite
    try:
        skills_path.write_text(_serialize_skills_toml(skills), encoding="utf-8")
    except Exception as exc:
        _dyn_log.error(f"Failed to rewrite knarr.skills.toml: {exc}")
        return False

    sentinel = config_dir / "knarr.reload"
    try:
        sentinel.touch()
    except Exception:
        pass

    _dyn_log.info(f"Removed dynamic skill: {skill_name}")
    return True
