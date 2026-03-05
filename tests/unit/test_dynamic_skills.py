"""Tests for v0.37.0 Track A1: Dynamic Skill Registration."""

import os
import tempfile
from pathlib import Path

import pytest

from knarr.cli.config import (
    get_dynamic_policy,
    load_config,
    load_dynamic_skills,
    validate_dynamic_skill,
    write_dynamic_skill,
    remove_dynamic_skill,
)


@pytest.fixture
def config_dir(tmp_path):
    """Create a temp config directory with minimal knarr.toml."""
    toml = tmp_path / "knarr.toml"
    toml.write_text(
        '[node]\nport = 9000\n\n'
        '[policy]\ndynamic_enabled = true\n'
        'dynamic_price_floor = 0.5\n'
        'dynamic_price_ceiling = 50.0\n'
        'max_dynamic_skills = 3\n'
    )
    return tmp_path


class TestGuardrails:
    """Test A1c: operator guardrails."""

    def test_price_below_floor(self, config_dir):
        policy = {"dynamic_enabled": True, "dynamic_price_floor": 1.0,
                  "dynamic_allowed_handlers": ["dynamic_facade.py"]}
        ok, reason = validate_dynamic_skill(
            "test-skill", {"handler": "dynamic_facade.py:handle", "price": 0.5},
            policy, 0,
        )
        assert not ok
        assert "below floor" in reason

    def test_price_above_ceiling(self, config_dir):
        policy = {"dynamic_enabled": True, "dynamic_price_ceiling": 10.0,
                  "dynamic_allowed_handlers": ["dynamic_facade.py"]}
        ok, reason = validate_dynamic_skill(
            "test-skill", {"handler": "dynamic_facade.py:handle", "price": 99.0},
            policy, 0,
        )
        assert not ok
        assert "above ceiling" in reason

    def test_max_skills_reached(self):
        policy = {"dynamic_enabled": True, "max_dynamic_skills": 5,
                  "dynamic_allowed_handlers": ["dynamic_facade.py"]}
        ok, reason = validate_dynamic_skill(
            "test-skill", {"handler": "dynamic_facade.py:handle", "price": 2.0},
            policy, 5,
        )
        assert not ok
        assert "max dynamic skills" in reason

    def test_handler_not_allowed(self):
        policy = {"dynamic_enabled": True, "dynamic_allowed_handlers": ["dynamic_facade.py"]}
        ok, reason = validate_dynamic_skill(
            "test-skill", {"handler": "evil.py:handle", "price": 2.0},
            policy, 0,
        )
        assert not ok
        assert "not in allowed list" in reason

    def test_disabled(self):
        policy = {"dynamic_enabled": False}
        ok, reason = validate_dynamic_skill(
            "test-skill", {"handler": "dynamic_facade.py:handle", "price": 2.0},
            policy, 0,
        )
        assert not ok
        assert "disabled" in reason

    def test_valid_passes(self):
        policy = {"dynamic_enabled": True, "dynamic_price_floor": 0.5,
                  "dynamic_price_ceiling": 50.0, "max_dynamic_skills": 10,
                  "dynamic_allowed_handlers": ["dynamic_facade.py"]}
        ok, reason = validate_dynamic_skill(
            "test-echo", {"handler": "dynamic_facade.py:handle", "price": 1.5},
            policy, 0,
        )
        assert ok
        assert reason == ""

    def test_invalid_skill_name(self):
        policy = {"dynamic_enabled": True, "dynamic_allowed_handlers": ["dynamic_facade.py"]}
        ok, reason = validate_dynamic_skill(
            "Test Skill!", {"handler": "dynamic_facade.py:handle", "price": 2.0},
            policy, 0,
        )
        assert not ok
        assert "invalid skill name" in reason

    def test_nan_price(self):
        policy = {"dynamic_enabled": True, "dynamic_allowed_handlers": ["dynamic_facade.py"]}
        ok, reason = validate_dynamic_skill(
            "test-skill", {"handler": "dynamic_facade.py:handle", "price": float("nan")},
            policy, 0,
        )
        assert not ok
        assert "invalid price" in reason


class TestTomlMerge:
    """Test A1a: knarr.skills.toml merged into config."""

    def test_dynamic_skills_merged(self, config_dir):
        # Write a dynamic skill
        skills_toml = config_dir / "knarr.skills.toml"
        skills_toml.write_text(
            '[skills.dynamic-echo]\n'
            'handler = "skills/dynamic_facade.py:handle"\n'
            'price = 1.5\n'
            'dynamic = true\n'
        )
        config = load_config(config_dir / "knarr.toml")
        assert "dynamic-echo" in config["skills"]
        assert config["skills"]["dynamic-echo"]["price"] == 1.5

    def test_static_not_overwritten(self, config_dir):
        # Add a static skill to knarr.toml
        toml = config_dir / "knarr.toml"
        toml.write_text(
            '[node]\nport = 9000\n\n'
            '[skills.echo]\nhandler = "skills/echo.py:handle"\nprice = 1.0\n'
        )
        # Write conflicting dynamic skill
        skills_toml = config_dir / "knarr.skills.toml"
        skills_toml.write_text(
            '[skills.echo]\n'
            'handler = "skills/dynamic_facade.py:handle"\n'
            'price = 5.0\n'
        )
        config = load_config(toml)
        # Static wins
        assert config["skills"]["echo"]["price"] == 1.0

    def test_no_dynamic_file(self, config_dir):
        config = load_config(config_dir / "knarr.toml")
        # Should work fine with empty skills
        assert isinstance(config.get("skills", {}), dict)


class TestWriteRemove:
    """Test write_dynamic_skill and remove_dynamic_skill."""

    def test_write_creates_file(self, config_dir):
        ok = write_dynamic_skill(config_dir, "test-echo", {
            "handler": "skills/dynamic_facade.py:handle",
            "price": 1.5,
            "source_skill": "echo",
            "source_provider": "abc123",
        })
        assert ok
        assert (config_dir / "knarr.skills.toml").is_file()
        assert (config_dir / "knarr.reload").exists()

        skills = load_dynamic_skills(config_dir)
        assert "test-echo" in skills

    def test_remove(self, config_dir):
        write_dynamic_skill(config_dir, "test-echo", {
            "handler": "skills/dynamic_facade.py:handle",
            "price": 1.5,
        })
        ok = remove_dynamic_skill(config_dir, "test-echo")
        assert ok
        skills = load_dynamic_skills(config_dir)
        assert "test-echo" not in skills

    def test_remove_nonexistent(self, config_dir):
        ok = remove_dynamic_skill(config_dir, "nope")
        assert not ok


class TestAutoDelistSweep:
    """Test A1e: TTL-based auto-delist (functional logic only)."""

    def test_stale_skill_detected(self, config_dir):
        """Skills with created_at older than TTL should be delist candidates."""
        import time
        # Write a skill with old created_at
        write_dynamic_skill(config_dir, "old-skill", {
            "handler": "skills/dynamic_facade.py:handle",
            "price": 1.5,
            "dynamic": True,
            "ttl_hours": 1,
            "created_at": "2020-01-01T00:00:00Z",
        })
        skills = load_dynamic_skills(config_dir)
        cfg = skills.get("old-skill", {})
        assert cfg.get("ttl_hours") == 1
        # In production, the sweep would check last_called > ttl
        # Here we just verify the field is preserved for sweep logic
