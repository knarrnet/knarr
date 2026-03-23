import json
import math
import re
from typing import Dict, Any, List, Optional
from .models import SkillSheet

class ValidationError(Exception):
    """Raised when skill sheet validation fails."""
    pass

MAX_SKILL_SHEET_SIZE = 4096  # 4KB
MAX_INPUT_SCHEMA_FULL_SIZE = 65536  # 64KB (SA-03)
MAX_TASK_INPUT_SIZE = 65536  # 64KB (SA-08)

# ADR-007: URI format — knarr:///category/subcategory/name@major.minor
# Three-slash = no authority (any provider). Two-slash with hex prefix = specific provider.
URI_PATTERN = re.compile(
    r'^knarr://([a-f0-9]{0,16})/([a-z0-9_-]+(/[a-z0-9_-]+)*/[a-z0-9_-]+)(@\d+(\.\d+)?)?$'
)
JURISDICTION_PATTERN = re.compile(r'^[a-z]{2}(\.[a-z]{2,})?$')
INPUT_SPEC_PATTERN = re.compile(r'^[a-f0-9]{64}$')

def validate_skill_sheet(data: Dict[str, Any]) -> SkillSheet:
    """
    Validates skill sheet data against requirements.
    
    Returns a SkillSheet object if valid, otherwise raises ValidationError.
    """
    # Size check (WITHOUT input_schema_full per spec)
    data_to_check = {k: v for k, v in data.items() if k != "input_schema_full"}
    serialized = json.dumps(data_to_check)
    if len(serialized.encode("utf-8")) > MAX_SKILL_SHEET_SIZE:
        raise ValidationError(f"Skill sheet exceeds maximum size of {MAX_SKILL_SHEET_SIZE} bytes")

    # SA-03: Validate input_schema_full size
    if "input_schema_full" in data and data["input_schema_full"]:
        full_schema_ser = json.dumps(data["input_schema_full"])
        if len(full_schema_ser.encode("utf-8")) > MAX_INPUT_SCHEMA_FULL_SIZE:
            raise ValidationError(f"input_schema_full exceeds maximum size of {MAX_INPUT_SCHEMA_FULL_SIZE} bytes")

    # Required fields
    required_fields = ["name", "version", "description", "tags", "input_schema", "output_schema"]
    for field in required_fields:
        if field not in data:
            raise ValidationError(f"Missing required field: {field}")

    # Type checks
    if not isinstance(data["name"], str):
        raise ValidationError("Field 'name' must be a string")
    if not isinstance(data["version"], str):
        raise ValidationError("Field 'version' must be a string")
    if not isinstance(data["description"], str):
        raise ValidationError("Field 'description' must be a string")
    if not isinstance(data["tags"], list):
        raise ValidationError("Field 'tags' must be a list of strings")
    if not isinstance(data["input_schema"], dict):
        raise ValidationError("Field 'input_schema' must be an object")
    if not isinstance(data["output_schema"], dict):
        raise ValidationError("Field 'output_schema' must be an object")

    # Name constraints: 1-64 chars, lowercase alphanumeric + hyphens
    name = data["name"].strip().lower()
    if not (1 <= len(name) <= 64):
        raise ValidationError("Skill name must be between 1 and 64 characters")
    if not re.match(r"^[a-z0-9-]+$", name):
        raise ValidationError("Skill name must contain only lowercase alphanumeric characters and hyphens")

    # Version constraint: semver (simplified check)
    if not re.match(r"^\d+\.\d+\.\d+$", data["version"]):
        raise ValidationError("Version must be in semver format (e.g., 1.0.0)")

    # Description constraints: 1-1024 chars (4KB total skill sheet limit is the real cap)
    if not (1 <= len(data["description"]) <= 1024):
        raise ValidationError("Description must be between 1 and 1024 characters")

    # Tags constraints: 1-10 tags, each 1-32 chars, lowercase alphanumeric + hyphens
    if not (1 <= len(data["tags"]) <= 10):
        raise ValidationError("Must have between 1 and 10 tags")
    for tag in data["tags"]:
        if not isinstance(tag, str):
            raise ValidationError("All tags must be strings")
        tag_clean = tag.strip().lower()
        if not (1 <= len(tag_clean) <= 32):
            raise ValidationError(f"Tag '{tag_clean}' must be between 1 and 32 characters")
        if not re.match(r"^[a-z0-9-]+$", tag_clean):
            raise ValidationError(f"Tag '{tag_clean}' must contain only lowercase alphanumeric characters and hyphens")

    # Schema constraints: flat string-valued dicts
    for schema_name in ["input_schema", "output_schema"]:
        schema = data[schema_name]
        for k, v in schema.items():
            if not isinstance(k, str):
                raise ValidationError(f"Schema key '{k}' in {schema_name} must be a string")
            if not isinstance(v, str):
                raise ValidationError(f"Schema value for '{k}' in {schema_name} must be a string type name")

    # Price validation [M-02] — ESC-02: allow negative prices (bounties).
    # Guard: must be a finite real number. Operator's dynamic_price_floor provides runtime guardrails.
    if "price" in data:
        price = data["price"]
        if not isinstance(price, (int, float)):
            raise ValidationError("Field 'price' must be a number")
        if math.isnan(price) or math.isinf(price):
            raise ValidationError("Field 'price' must be a finite number")
        if price > 1000.0:
            raise ValidationError("Field 'price' must not exceed 1000.0")

    # max_input_size validation
    if "max_input_size" in data:
        mis = data["max_input_size"]
        if not isinstance(mis, int) or mis <= 0 or mis > 10485760:  # max 10MB
            raise ValidationError("Field 'max_input_size' must be integer between 1 and 10485760")

    # ADR-007: URI validation
    if "uri" in data and data["uri"]:
        if not isinstance(data["uri"], str):
            raise ValidationError("uri must match knarr:///category/name[@version] format")
        if len(data["uri"]) > 256:
            raise ValidationError("uri must not exceed 256 characters")
        if not URI_PATTERN.match(data["uri"]):
            raise ValidationError("uri must match knarr:///category/name[@version] format")

    # input_spec validation (sidecar asset hash)
    if "input_spec" in data and data["input_spec"]:
        if not isinstance(data["input_spec"], str) or not INPUT_SPEC_PATTERN.match(data["input_spec"]):
            raise ValidationError("input_spec must be a 64-character hex SHA-256 hash")

    # Jurisdiction validation [E-3]
    if "jurisdiction" in data and data["jurisdiction"]:
        # Auto-wrap bare string to list (BUG-005: common config mistake)
        if isinstance(data["jurisdiction"], str):
            data["jurisdiction"] = [data["jurisdiction"]]
        if not isinstance(data["jurisdiction"], list) or len(data["jurisdiction"]) > 10:
            raise ValidationError("jurisdiction must be a list of max 10 strings")
        for j in data["jurisdiction"]:
            if not isinstance(j, str) or not JURISDICTION_PATTERN.match(j):
                raise ValidationError(f"jurisdiction '{j}' must be lowercase country[.region] format")

    return SkillSheet.from_dict(data)

def validate_task_input(input_data: Dict[str, Any], input_schema: Dict[str, str], 
                        max_size: int = MAX_TASK_INPUT_SIZE) -> Optional[Dict[str, Any]]:
    """
    Validates task input data against a skill's input schema and size limit.
    Returns None if valid, or a structured error dict if invalid.
    """
    # SA-08: Validate input size (parameterized)
    try:
        serialized = json.dumps(input_data)
        if len(serialized.encode("utf-8")) > max_size:
            return {
                "code": "INPUT_TOO_LARGE",
                "message": f"Input data exceeds maximum size of {max_size} bytes"
            }
    except Exception:
        # Should not happen if input_data is JSON-serializable dict
        pass

    required_keys = input_schema.get("_required", list(input_schema.keys()))
    missing = [k for k in required_keys if k not in input_data]
    if missing:
        return {
            "code": "INVALID_INPUT",
            "message": f"Missing required fields: {', '.join(missing)}",
            "detail": {"missing_fields": missing}
        }
    return None

def match_uri_version(requested_uri: str, candidate_uri: str) -> bool:
    """Semver-compatible URI matching.
    @1 matches any 1.x. @1.2 matches exactly. No version = any version."""
    req_base, _, req_ver = requested_uri.rpartition('@')
    cand_base, _, cand_ver = candidate_uri.rpartition('@')
    if not req_base:
        # No '@' in requested — treat entire string as base
        req_base = requested_uri
        req_ver = ""
    if not cand_base:
        cand_base = candidate_uri
        cand_ver = ""
    if req_base != cand_base:
        return False
    if not req_ver:  # No version requested = match all
        return True
    if '.' not in req_ver:  # Major only: @1 matches 1.anything
        return cand_ver.split('.')[0] == req_ver
    return cand_ver == req_ver  # Exact match


def flatten_json_schema(json_schema: Dict[str, Any]) -> Dict[str, str]:
    """Flatten JSON Schema properties to {field_name: type_string}."""
    flat = {}
    properties = json_schema.get("properties", {})
    if not properties:
        return {}
        
    for key, prop in properties.items():
        prop_type = prop.get("type", "string")
        if isinstance(prop_type, list):
            # Union types like ["string", "null"] -> take first non-null
            prop_type = next((t for t in prop_type if t != "null"), "string")
        
        if prop_type == "object":
            flat[key] = "object"
        elif prop_type == "array":
            flat[key] = "array"
        else:
            flat[key] = str(prop_type)
            
    return flat
