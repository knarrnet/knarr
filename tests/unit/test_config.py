import pytest
import os
import tomllib
from pathlib import Path
from knarr.cli.config import load_config, merge_defaults, DEFAULT_CONFIG

def test_merge_defaults():
    defaults = {"a": 1, "b": {"c": 2, "d": 3}}
    overrides = {"b": {"c": 20}, "e": 5}
    merged = merge_defaults(defaults, overrides)
    assert merged["a"] == 1
    assert merged["b"]["c"] == 20
    assert merged["b"]["d"] == 3
    assert merged["e"] == 5

def test_load_config_valid(tmp_path):
    config_file = tmp_path / "knarr.toml"
    config_file.write_text("""
[node]
port = 1234
host = "1.2.3.4"

[network]
bootstrap = ["p1", "p2"]
""")
    config = load_config(config_file)
    assert config["node"]["port"] == 1234
    assert config["node"]["host"] == "1.2.3.4"
    assert config["network"]["bootstrap"] == ["p1", "p2"]
    # Check default was kept
    assert config["node"]["storage"] == "node.db"

def test_load_config_missing(tmp_path):
    config = load_config(tmp_path / "nonexistent.toml")
    assert config == DEFAULT_CONFIG

def test_load_config_explicit_missing(tmp_path):
    with pytest.raises(SystemExit):
        load_config(tmp_path / "explicit-missing.toml", explicit=True)

