"""Tests for Track B1: Config File Splitting by Security Tier (v0.40.0).

Tests that load_config() correctly discovers and deep-merges tier files
(knarr.economy.toml, knarr.skills.toml, knarr.mail.toml) without breaking
single-file behavior.
"""
import sys
import os
import io
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from knarr.cli.config import load_config, deep_merge


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _write_toml(directory: Path, filename: str, content: str) -> Path:
    """Write a TOML file and return its path."""
    p = directory / filename
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# B1-T1: Single file — behavior identical to pre-v0.40.0
# ---------------------------------------------------------------------------

def test_single_file_unchanged(tmp_path):
    cfg_path = _write_toml(tmp_path, "knarr.toml", """
[node]
port = 9001
host = "127.0.0.1"

[mail]
accept_from = "whitelist"
""")

    cfg = load_config(cfg_path)
    assert cfg["node"]["port"] == 9001
    assert cfg["node"]["host"] == "127.0.0.1"
    assert cfg["mail"]["accept_from"] == "whitelist"


# ---------------------------------------------------------------------------
# B1-T2: Economy tier file merges its sections into config
# ---------------------------------------------------------------------------

def test_economy_tier_file_merges(tmp_path):
    _write_toml(tmp_path, "knarr.toml", """
[node]
port = 9000
""")
    _write_toml(tmp_path, "knarr.economy.toml", """
[economy]
credit_limit_default = 100.0

[settlement]
threshold = 0.8
""")

    cfg = load_config(tmp_path / "knarr.toml")
    assert cfg["economy"]["credit_limit_default"] == 100.0
    assert cfg["settlement"]["threshold"] == 0.8
    # node section from base is preserved
    assert cfg["node"]["port"] == 9000


# ---------------------------------------------------------------------------
# B1-T3: Key-level override — tier file overrides individual keys, not whole sections
# ---------------------------------------------------------------------------

def test_key_level_override(tmp_path):
    _write_toml(tmp_path, "knarr.toml", """
[settlement]
threshold = 0.6
cadence = 3600
strategy = "utilization"
""")
    _write_toml(tmp_path, "knarr.economy.toml", """
[settlement]
threshold = 0.8
""")

    cfg = load_config(tmp_path / "knarr.toml")
    # Override wins
    assert cfg["settlement"]["threshold"] == 0.8
    # Other keys are preserved
    assert cfg["settlement"]["cadence"] == 3600
    assert cfg["settlement"]["strategy"] == "utilization"


# ---------------------------------------------------------------------------
# B1-T4: Missing tier files — no error, config unchanged
# ---------------------------------------------------------------------------

def test_missing_tier_files_no_error(tmp_path):
    _write_toml(tmp_path, "knarr.toml", """
[node]
port = 9999
""")
    # No tier files exist in tmp_path
    cfg = load_config(tmp_path / "knarr.toml")
    assert cfg["node"]["port"] == 9999


# ---------------------------------------------------------------------------
# B1-T5: [settlement] at root level in economy file works
# ---------------------------------------------------------------------------

def test_settlement_at_root_in_economy_file(tmp_path):
    _write_toml(tmp_path, "knarr.toml", """
[node]
port = 9000
""")
    _write_toml(tmp_path, "knarr.economy.toml", """
[settlement]
threshold = 0.75
cadence = 1800
""")

    cfg = load_config(tmp_path / "knarr.toml")
    assert cfg["settlement"]["threshold"] == 0.75
    assert cfg["settlement"]["cadence"] == 1800


# ---------------------------------------------------------------------------
# B1-T6: Unknown keys in tier files produce tier-aware warnings
# ---------------------------------------------------------------------------

def test_unknown_keys_tier_aware(tmp_path, capsys):
    _write_toml(tmp_path, "knarr.toml", """
[node]
port = 9000
""")
    _write_toml(tmp_path, "knarr.economy.toml", """
[economy]
credit_limit_default = 50.0

[node]
port = 9001
""")

    load_config(tmp_path / "knarr.toml")

    captured = capsys.readouterr()
    # [node] is not a valid section in knarr.economy.toml
    assert "knarr.economy.toml" in captured.err
    assert "[node]" in captured.err or "node" in captured.err


# ---------------------------------------------------------------------------
# B1-T7: Mail tier file merges [mail] section
# ---------------------------------------------------------------------------

def test_mail_tier_file_merges(tmp_path):
    _write_toml(tmp_path, "knarr.toml", """
[node]
port = 9000

[mail]
accept_from = "all"
max_messages = 5000
""")
    _write_toml(tmp_path, "knarr.mail.toml", """
[mail]
max_messages = 10000
""")

    cfg = load_config(tmp_path / "knarr.toml")
    assert cfg["mail"]["max_messages"] == 10000
    # Other mail keys preserved
    assert cfg["mail"]["accept_from"] == "all"


# ---------------------------------------------------------------------------
# B1-T8: All three tier files can coexist
# ---------------------------------------------------------------------------

def test_all_tier_files_coexist(tmp_path):
    _write_toml(tmp_path, "knarr.toml", """
[node]
port = 9000
""")
    _write_toml(tmp_path, "knarr.economy.toml", """
[economy]
credit_limit_default = 200.0
""")
    _write_toml(tmp_path, "knarr.skills.toml", """
[skills.echo]
handler = "builtin:echo"
price = 2.0
""")
    _write_toml(tmp_path, "knarr.mail.toml", """
[mail]
max_messages = 99999
""")

    cfg = load_config(tmp_path / "knarr.toml")
    assert cfg["economy"]["credit_limit_default"] == 200.0
    assert cfg["skills"]["echo"]["price"] == 2.0
    assert cfg["mail"]["max_messages"] == 99999
    assert cfg["node"]["port"] == 9000


# ---------------------------------------------------------------------------
# B1-T9: deep_merge unit test — recursive merge works correctly
# ---------------------------------------------------------------------------

def test_deep_merge_recursive():
    base = {"a": {"x": 1, "y": 2}, "b": 10}
    override = {"a": {"y": 99, "z": 3}, "c": 20}

    result = deep_merge(base, override)
    assert result["a"]["x"] == 1      # preserved
    assert result["a"]["y"] == 99     # overridden
    assert result["a"]["z"] == 3      # new from override
    assert result["b"] == 10          # preserved
    assert result["c"] == 20          # new from override


def test_deep_merge_leaf_override():
    base = {"key": "original"}
    override = {"key": "new"}
    result = deep_merge(base, override)
    assert result["key"] == "new"


def test_deep_merge_does_not_mutate_base():
    base = {"a": {"x": 1}}
    override = {"a": {"y": 2}}
    result = deep_merge(base, override)
    # base is unchanged
    assert "y" not in base["a"]
    assert result["a"]["y"] == 2
