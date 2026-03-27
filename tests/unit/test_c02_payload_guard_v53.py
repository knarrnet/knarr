"""C-02: Thrall payload guard (D's implementation — guard_payload function).

Tests:
1. Small payloads (under limit) pass through unchanged.
2. Over-limit payloads have skip_fields stripped.
3. Record list is truncated as last resort.
4. Empty input is returned unchanged.
5. Custom limit is respected.
6. Custom skip_fields are used.
"""
import json
import pytest


class TestPayloadGuard:
    def test_small_payload_unchanged(self):
        """Small payload (under limit) is returned unchanged."""
        from knarr.core.payload_guard import guard_payload
        records = [{"key": "value", "count": 42}]
        result = guard_payload(records)
        assert result == records

    def test_empty_input(self):
        """Empty list is returned unchanged."""
        from knarr.core.payload_guard import guard_payload
        assert guard_payload([]) == []

    def test_over_limit_strips_skip_fields(self):
        """When over limit, default skip_fields are stripped."""
        from knarr.core.payload_guard import guard_payload
        big = "x" * 300_000  # ~300KB > 256KB
        records = [{"data": "keep", "raw_output": big, "name": "test"}]
        result = guard_payload(records)
        # raw_output is in _SKIP_FIELDS — should be removed
        assert "raw_output" not in result[0]
        assert result[0]["data"] == "keep"
        assert result[0]["name"] == "test"

    def test_over_limit_truncates_record_list(self):
        """When stripping fields is insufficient, truncate record list."""
        from knarr.core.payload_guard import guard_payload
        # Create many records that exceed limit with no skip_fields
        records = [{"data": "a" * 50_000} for _ in range(10)]  # ~500KB
        result = guard_payload(records, limit=100_000, skip_fields=[])
        assert len(result) < 10  # must have truncated

    def test_custom_limit(self):
        """Custom limit is respected."""
        from knarr.core.payload_guard import guard_payload
        records = [{"data": "x" * 500}]
        # Limit is tiny — but records fit
        result = guard_payload(records, limit=1000)
        assert result == records

    def test_custom_limit_triggers_trim(self):
        """Custom small limit triggers field stripping."""
        from knarr.core.payload_guard import guard_payload
        records = [{"data": "keep", "raw_output": "x" * 1000}]
        result = guard_payload(records, limit=500)
        assert "raw_output" not in result[0]

    def test_custom_skip_fields(self):
        """Custom skip_fields are used instead of defaults."""
        from knarr.core.payload_guard import guard_payload
        records = [{"data": "keep", "my_big_field": "x" * 300_000}]
        result = guard_payload(records, skip_fields=["my_big_field"])
        assert "my_big_field" not in result[0]
        assert result[0]["data"] == "keep"

    def test_debug_logging(self):
        """debug=True does not crash."""
        from knarr.core.payload_guard import guard_payload
        records = [{"data": "keep", "raw_output": "x" * 300_000}]
        result = guard_payload(records, debug=True)
        assert "raw_output" not in result[0]

    def test_default_limit_256kb(self):
        """Default limit is 256KB."""
        from knarr.core.payload_guard import DEFAULT_PAYLOAD_LIMIT
        assert DEFAULT_PAYLOAD_LIMIT == 256 * 1024

    def test_non_dict_records_survive(self):
        """Records that aren't dicts are not modified."""
        from knarr.core.payload_guard import guard_payload
        records = ["string_record", 42]
        result = guard_payload(records, limit=5)
        # Can't strip fields from non-dicts, but should not crash
        assert isinstance(result, list)
