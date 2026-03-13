"""Watchman configuration — loads and validates watchman.toml."""
from __future__ import annotations

import os
from typing import Any, Dict

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore

_DEFAULTS: Dict[str, Any] = {
    "node": {
        "command": "knarr",
        "args": ["serve"],
        "data_dir": "",
    },
    "health": {
        "health_interval": 10,
        "health_fail_threshold": 3,
        "cockpit_url": "http://127.0.0.1:8080",
    },
    "recovery": {
        "max_restarts": 10,
        "initial_backoff": 5,
        "max_backoff": 300,
        "backoff_reset_uptime": 1800,
    },
    "upgrade": {
        "auto_upgrade": False,
        "check_interval": 3600,
        "drain_timeout": 60,
        "health_timeout": 30,
        "source": "github:knarrnet/knarr",
    },
    "plugins": {
        "sync_plugins": False,
        "plugin_dir": "plugins",
    },
}


def _deep_merge(base: Dict, override: Dict) -> Dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(path: str) -> Dict[str, Any]:
    """Load watchman.toml, merging with defaults. Returns config dict."""
    cfg: Dict[str, Any] = {}
    if os.path.exists(path):
        if tomllib is None:
            raise RuntimeError(
                "TOML parser not available. Install tomli (Python <3.11) or use Python >=3.11."
            )
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    return _deep_merge(_DEFAULTS, cfg)
