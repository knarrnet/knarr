from knarr.core.validation import flatten_json_schema

def test_flatten_json_schema_simple():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "number"},
            "active": {"type": "boolean"}
        }
    }
    flat = flatten_json_schema(schema)
    assert flat == {"name": "string", "age": "number", "active": "boolean"}

def test_flatten_json_schema_nested():
    schema = {
        "type": "object",
        "properties": {
            "meta": {"type": "object", "properties": {"id": {"type": "string"}}},
            "items": {"type": "array", "items": {"type": "string"}}
        }
    }
    flat = flatten_json_schema(schema)
    assert flat == {"meta": "object", "items": "array"}

def test_flatten_json_schema_union():
    schema = {
        "type": "object",
        "properties": {
            "optional": {"type": ["string", "null"]}
        }
    }
    flat = flatten_json_schema(schema)
    assert flat == {"optional": "string"}

def test_flatten_json_schema_empty():
    assert flatten_json_schema({}) == {}
    assert flatten_json_schema({"type": "object"}) == {}
