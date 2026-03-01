from knarr.core.validation import validate_task_input

def test_validate_task_input_valid():
    schema = {"text": "string", "count": "int"}
    data = {"text": "hello", "count": 1, "extra": "ignored"}
    assert validate_task_input(data, schema) is None

def test_validate_task_input_missing():
    schema = {"text": "string", "count": "int"}
    data = {"text": "hello"}
    error = validate_task_input(data, schema)
    assert error is not None
    assert error["code"] == "INVALID_INPUT"
    assert "count" in error["detail"]["missing_fields"]
