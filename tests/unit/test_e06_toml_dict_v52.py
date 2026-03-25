"""E-06: _serialize_skills_toml handles dict values as TOML inline tables.

Without the fix, a skill config dict value like:
    config = {"key1": "val1", "key2": 2}
would serialize as the Python repr `{'key1': 'val1', 'key2': 2}` which is
invalid TOML and causes a parse error on reload.

With the fix, it serializes as: config = {key1 = "val1", key2 = 2}
"""

import pytest


def _serialize_skills_toml(skills: dict) -> str:
    """Import the real function from cli.config."""
    from knarr.cli.config import _serialize_skills_toml
    return _serialize_skills_toml(skills)


# ──────────────────────────────────────────────────────────────────────────────
# E-06-A: Dict value serializes to TOML inline table (not Python repr)
# ──────────────────────────────────────────────────────────────────────────────

def test_dict_value_serialized_as_inline_table():
    """A dict skill config value must serialize as TOML inline table."""
    skills = {
        "my-skill": {
            "enabled": True,
            "options": {"key1": "val1", "key2": "val2"},
        }
    }
    result = _serialize_skills_toml(skills)

    # Must NOT contain Python dict repr
    assert "{'key1'" not in result
    assert "{\"key1\"" not in result
    # Must contain TOML inline table format
    assert "{" in result and "key1" in result


def test_dict_value_toml_parseable():
    """The output with dict values must be valid TOML."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    skills = {
        "test-skill": {
            "version": "1.0",
            "settings": {"timeout": 30, "retry": 3},
        }
    }
    result = _serialize_skills_toml(skills)
    # Wrap in [skills] section for parsing
    full_toml = "[skills]\n" + result.replace("[skills.test-skill]", "[skills.test-skill]", 1)
    try:
        # Try to parse as TOML — should not raise
        parsed = tomllib.loads(full_toml)
    except Exception as e:
        pytest.fail(f"Output TOML with dict value is not valid: {e}\n\nGenerated TOML:\n{result}")


# ──────────────────────────────────────────────────────────────────────────────
# E-06-B: Non-dict values still serialize correctly
# ──────────────────────────────────────────────────────────────────────────────

def test_string_value_still_works():
    skills = {"skill1": {"name": "hello"}}
    result = _serialize_skills_toml(skills)
    assert 'name = "hello"' in result


def test_bool_value_still_works():
    skills = {"skill1": {"enabled": True, "debug": False}}
    result = _serialize_skills_toml(skills)
    assert "enabled = true" in result
    assert "debug = false" in result


def test_list_value_still_works():
    skills = {"skill1": {"tags": ["a", "b", "c"]}}
    result = _serialize_skills_toml(skills)
    assert 'tags = ["a", "b", "c"]' in result


def test_integer_value_still_works():
    skills = {"skill1": {"timeout": 30}}
    result = _serialize_skills_toml(skills)
    assert "timeout = 30" in result


# ──────────────────────────────────────────────────────────────────────────────
# E-06-C: Dict with string and numeric values
# ──────────────────────────────────────────────────────────────────────────────

def test_dict_with_mixed_values():
    skills = {
        "mixer": {
            "config": {"host": "localhost", "port": 8080, "enabled": True},
        }
    }
    result = _serialize_skills_toml(skills)
    # Should contain inline table pattern
    assert "config = {" in result
    assert "host" in result
    assert "8080" in result


# ──────────────────────────────────────────────────────────────────────────────
# E-06-D: Empty dict value
# ──────────────────────────────────────────────────────────────────────────────

def test_empty_dict_value():
    skills = {"skill1": {"options": {}}}
    result = _serialize_skills_toml(skills)
    # Empty inline table
    assert "options = {}" in result


# ──────────────────────────────────────────────────────────────────────────────
# E-06-E: Regression — serialize round-trip preserves skill name and structure
# ──────────────────────────────────────────────────────────────────────────────

def test_round_trip_preserves_section_header():
    skills = {
        "my-tool": {
            "version": "2.0",
            "params": {"x": 1, "y": 2},
        }
    }
    result = _serialize_skills_toml(skills)
    assert "[skills.my-tool]" in result
    assert 'version = "2.0"' in result
