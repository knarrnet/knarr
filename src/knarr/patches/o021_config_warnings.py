"""
O-021: Config parser warnings fix.

Root cause: _warn_unknown_keys() warns for nested TOML tables like [skills.llm-chat],
treating per-skill sub-tables as unknown keys.

Fix: Skip warning for nested tables (dicts) under validated sections.

Integration point: Replace lines 76-82 in cli/config.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Set


def warn_unknown_keys(
    raw: dict,
    path: Path,
    known_keys: Dict[str, Set[str]],
) -> int:
    """Warn about unrecognized keys in known sections to catch typos.

    Skips nested tables (dicts) — these are per-item sub-configs
    like [skills.llm-chat] or [policy.skill.echo].

    Returns count of warnings emitted (useful for testing).
    """
    warnings = 0
    for section, known in known_keys.items():
        if section in raw and isinstance(raw[section], dict):
            for key in raw[section]:
                if key not in known and not isinstance(raw[section][key], dict):
                    print(
                        f"Warning: Unknown key '{key}' in [{section}] in {path}",
                        file=sys.stderr,
                    )
                    warnings += 1
    return warnings
