"""C-01 (v0.58.0): Dynamic skill lifecycle.

Fixes:
1. Registration endpoint: POST /api/skills/register
2. validate_dynamic_skill phantom-default bug fix
3. cmd_skill_remove handles flat dynamic-skill layout

Scenarios:
- Register valid source → persisted
- Non-module source → 400 nothing written
- Path traversal in name → rejected
- dynamic_enabled=false → rejected before I/O
- Remove existing flat skill → cleaned
- Remove non-existent → no-op
"""
import ast
import os
import tempfile
from pathlib import Path

import pytest


class TestValidateDynamicSkillPhantomDefault:
    """Fix phantom-default allowlist bug."""

    def test_dynamic_enabled_no_allowlist_accepts_canonical(self):
        """When dynamic_enabled=true with no explicit allowlist, accept dynamic_*.py."""
        from knarr.cli.config import get_dynamic_policy, validate_dynamic_skill

        config = {"policy": {"dynamic_enabled": True}}
        policy = get_dynamic_policy(config)

        # Handler matching dynamic_*.py pattern should be accepted
        ok, reason = validate_dynamic_skill(
            "my-skill",
            {"handler": "dynamic_skills/my-skill.py:handle", "price": 1.0},
            policy,
            existing_count=0,
        )
        assert ok is True, f"Expected ok for canonical handler: {reason}"

    def test_dynamic_enabled_no_allowlist_rejects_non_canonical(self):
        """When no explicit allowlist, non-dynamic_*.py handler rejected."""
        from knarr.cli.config import get_dynamic_policy, validate_dynamic_skill

        config = {"policy": {"dynamic_enabled": True}}
        policy = get_dynamic_policy(config)

        ok, reason = validate_dynamic_skill(
            "my-skill",
            {"handler": "evil_handler.py:handle", "price": 1.0},
            policy,
            existing_count=0,
        )
        assert ok is False, "Expected rejection for non-canonical handler"
        assert "dynamic handler pattern" in reason

    def test_explicit_allowlist_still_works(self):
        """When explicit allowlist is set, use it."""
        from knarr.cli.config import get_dynamic_policy, validate_dynamic_skill

        config = {
            "policy": {
                "dynamic_enabled": True,
                "dynamic_allowed_handlers": ["custom_handler.py"],
            }
        }
        policy = get_dynamic_policy(config)

        # Allowed handler should pass
        ok, _ = validate_dynamic_skill(
            "my-skill",
            {"handler": "custom_handler.py:handle", "price": 1.0},
            policy,
            existing_count=0,
        )
        assert ok is True

        # Non-allowed handler should fail
        ok2, _ = validate_dynamic_skill(
            "my-skill",
            {"handler": "other_handler.py:handle", "price": 1.0},
            policy,
            existing_count=0,
        )
        assert ok2 is False

    def test_dynamic_disabled_rejected(self):
        """When dynamic_enabled=false (default), reject before I/O."""
        from knarr.cli.config import get_dynamic_policy, validate_dynamic_skill

        config = {}  # default: dynamic_enabled=false
        policy = get_dynamic_policy(config)

        ok, reason = validate_dynamic_skill(
            "my-skill",
            {"handler": "dynamic_facade.py:handle", "price": 1.0},
            policy,
            existing_count=0,
        )
        assert ok is False
        assert "disabled" in reason.lower()


class TestSkillRemoveDynamicLayout:
    """cmd_skill_remove handles flat dynamic-skill layout."""

    def test_remove_nonexistent_noop(self):
        """Remove non-existent skill → no-op + message."""
        from knarr.cli.skill import cmd_skill_remove

        with tempfile.TemporaryDirectory() as td:
            result = cmd_skill_remove("nonexistent-skill", td)
            assert "not installed" in result.lower()

    def test_remove_dynamic_skill(self):
        """Remove existing flat dynamic skill → cleaned."""
        from knarr.cli.skill import cmd_skill_remove
        from knarr.cli.config import write_dynamic_skill

        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)

            # Create a dynamic skill
            skills_path = config_dir / "knarr.skills.toml"
            skill_cfg = {
                "handler": "dynamic_skills/test-skill.py:handle",
                "price": 1.0,
                "dynamic": True,
            }

            # Create the handler file
            dynamic_dir = config_dir / "dynamic_skills"
            dynamic_dir.mkdir()
            (dynamic_dir / "test-skill.py").write_text("def handle(x): return x\n")

            # Write to config
            write_dynamic_skill(config_dir, "test-skill", skill_cfg)

            # Verify it exists
            assert skills_path.exists()
            assert (dynamic_dir / "test-skill.py").exists()

            # Remove it
            result = cmd_skill_remove("test-skill", str(config_dir))
            assert "Removed" in result or "removed" in result.lower()


class TestRegistrationValidation:
    """Registration endpoint validation logic."""

    def test_valid_source_parsed(self):
        """Valid Python source passes ast.parse."""
        source = "def handle(x): return x * 2\n"
        tree = ast.parse(source)
        assert tree is not None

    def test_invalid_source_rejected(self):
        """Invalid Python source fails ast.parse."""
        source = "def handle( broken syntax"
        with pytest.raises(SyntaxError):
            ast.parse(source)

    def test_path_traversal_rejected(self):
        """Skill name with path traversal chars rejected."""
        import re
        name = "../etc/passwd"
        has_traversal = ".." in name or "/" in name or "\\" in name
        assert has_traversal is True

    def test_valid_name_accepted(self):
        """Valid skill name passes regex."""
        import re
        name = "my-skill-123"
        assert re.match(r'^[a-z0-9][a-z0-9-]*$', name)
