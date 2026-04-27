"""C-02: Payload guard — limit gather payload size with selective field skipping.

Standalone module. Used by thrall plugin and any gather-like aggregation.
Winner: D (v0.53.0 rulings).
"""
import json
import logging

logger = logging.getLogger(__name__)

# Default maximum payload size in bytes (256KB)
DEFAULT_PAYLOAD_LIMIT = 256 * 1024

# Fields to skip when payload exceeds limit, in priority order (largest first)
_SKIP_FIELDS = ["raw_output", "debug_trace", "full_response", "extended_data"]


def guard_payload(
    records: list,
    limit: int = DEFAULT_PAYLOAD_LIMIT,
    skip_fields: list | None = None,
    debug: bool = False,
) -> list:
    """Guard a list of records against payload size limits.

    If the serialized size exceeds `limit`, iteratively remove large fields
    from individual records until under limit. Returns the (possibly trimmed)
    records list.

    Args:
        records: List of dicts to be serialized.
        limit: Maximum total payload size in bytes (configurable by caller).
        skip_fields: Fields to strip when over limit (in priority order).
        debug: Enable debug logging.

    Returns:
        List of records, possibly with some fields removed.
    """
    if not records:
        return records

    if skip_fields is None:
        skip_fields = _SKIP_FIELDS

    try:
        size = len(json.dumps(records).encode("utf-8"))
    except (TypeError, ValueError):
        return records

    if size <= limit:
        return records

    if debug:
        logger.info(
            "PAYLOAD_GUARD_TRIM size=%d limit=%d records=%d",
            size, limit, len(records),
        )

    # A-03: shallow-copy dict records before mutating so the caller's input
    # is not modified in place. List is already reassignable below for truncation.
    records = [dict(r) if isinstance(r, dict) else r for r in records]

    # Iteratively strip fields to reduce size
    for field_name in skip_fields:
        for record in records:
            if isinstance(record, dict):
                record.pop(field_name, None)
        try:
            size = len(json.dumps(records).encode("utf-8"))
        except (TypeError, ValueError):
            break
        if size <= limit:
            if debug:
                logger.info(
                    "PAYLOAD_GUARD_OK size=%d after_strip=%s",
                    size, field_name,
                )
            return records

    # If still over limit after stripping fields, truncate record list
    while len(records) > 1:
        records = records[:-1]
        try:
            size = len(json.dumps(records).encode("utf-8"))
        except (TypeError, ValueError):
            break
        if size <= limit:
            if debug:
                logger.info(
                    "PAYLOAD_GUARD_TRUNCATED size=%d records=%d",
                    size, len(records),
                )
            return records

    return records
