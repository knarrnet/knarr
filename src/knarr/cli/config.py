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
    """Load and validate knarr.toml. Returns merged config with defaults."""
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
        return merge_defaults(DEFAULT_CONFIG, raw)
    except tomllib.TOMLDecodeError as e:
        print(f"Error: Invalid TOML in {path}: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error in {path}: {e}", file=sys.stderr)
        print("Check the configuration reference in the README.", file=sys.stderr)
        sys.exit(1)

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
    "policy": {"initial_credit", "min_balance", "tit_for_tat", "group", "skill"},
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
