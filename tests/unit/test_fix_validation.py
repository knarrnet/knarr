import pytest
from knarr.core.validation import validate_task_input

def test_optional_fields_validation():
    # Schema with _required override
    schema = {
        "text": "string",
        "optional_field": "string",
        "_required": ["text"]
    }
    
    # Valid: optional_field missing
    err = validate_task_input({"text": "hello"}, schema)
    assert err is None
    
    # Invalid: required text missing
    err = validate_task_input({"optional_field": "hi"}, schema)
    assert err is not None
    assert "Missing required fields: text" in err["message"]

def test_default_all_required_validation():
    # Standard schema (no _required key)
    schema = {
        "text": "string",
        "other": "string"
    }
    
    # Invalid: missing 'other'
    err = validate_task_input({"text": "hello"}, schema)
    assert err is not None
    assert "other" in err["message"]
