import pytest
from knarr.core.validation import validate_skill_sheet, ValidationError

def test_validate_valid_sheet():
    data = {
        "name": "test-skill",
        "version": "1.0.0",
        "description": "A test skill",
        "tags": ["test", "demo"],
        "input_schema": {"text": "string"},
        "output_schema": {"result": "string"}
    }
    sheet = validate_skill_sheet(data)
    assert sheet.name == "test-skill"

def test_validate_missing_field():
    data = {"name": "test"}
    with pytest.raises(ValidationError, match="Missing required field"):
        validate_skill_sheet(data)

def test_validate_invalid_name():
    data = {
        "name": "Invalid Name!",
        "version": "1.0.0",
        "description": "test",
        "tags": ["tag"],
        "input_schema": {},
        "output_schema": {}
    }
    with pytest.raises(ValidationError, match="Skill name must contain only lowercase alphanumeric"):
        validate_skill_sheet(data)

def test_validate_oversized():
    data = {
        "name": "test",
        "version": "1.0.0",
        "description": "a" * 5000,
        "tags": ["tag"],
        "input_schema": {},
        "output_schema": {}
    }
    with pytest.raises(ValidationError, match="exceeds maximum size"):
        validate_skill_sheet(data)
