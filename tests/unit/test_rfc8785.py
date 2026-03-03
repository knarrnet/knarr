"""Tests for vendored RFC 8785 (JCS) implementation."""

import math
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from knarr.core.rfc8785 import dumps, CanonicalizationError, FloatDomainError, IntegerDomainError


class TestDumps:
    def test_empty_object(self):
        assert dumps({}) == b"{}"

    def test_empty_array(self):
        assert dumps([]) == b"[]"

    def test_null(self):
        assert dumps(None) == b"null"

    def test_bool_true(self):
        assert dumps(True) == b"true"

    def test_bool_false(self):
        assert dumps(False) == b"false"

    def test_integer(self):
        assert dumps(42) == b"42"

    def test_negative_integer(self):
        assert dumps(-1) == b"-1"

    def test_string(self):
        assert dumps("hello") == b'"hello"'

    def test_key_sorting(self):
        """Keys sorted by UTF-16BE encoding."""
        assert dumps({"b": 1, "a": 2}) == b'{"a":2,"b":1}'

    def test_nested_object(self):
        result = dumps({"z": {"b": 1, "a": 2}, "a": 3})
        assert result == b'{"a":3,"z":{"a":2,"b":1}}'

    def test_array_with_mixed_types(self):
        result = dumps([1, "two", None, True, False])
        assert result == b'[1,"two",null,true,false]'

    def test_no_trailing_zeros_on_float(self):
        """1.0 should serialize as '1' (no trailing .0)."""
        assert dumps(1.0) == b"1"

    def test_float_precision(self):
        assert dumps(1.5) == b"1.5"

    def test_no_spaces(self):
        """JCS uses minimal separators — no spaces."""
        result = dumps({"key": [1, 2, 3]})
        assert b" " not in result

    def test_string_escaping(self):
        assert dumps("a\nb") == b'"a\\nb"'
        assert dumps("a\tb") == b'"a\\tb"'

    def test_nan_raises(self):
        with pytest.raises(FloatDomainError):
            dumps(float("nan"))

    def test_inf_raises(self):
        with pytest.raises(FloatDomainError):
            dumps(float("inf"))

    def test_integer_domain_max(self):
        with pytest.raises(IntegerDomainError):
            dumps(2**53)

    def test_integer_domain_min(self):
        with pytest.raises(IntegerDomainError):
            dumps(-(2**53))

    def test_non_string_key_raises(self):
        with pytest.raises(CanonicalizationError):
            dumps({1: "value"})

    def test_deterministic(self):
        """Same input always produces same output."""
        obj = {"c": 3, "a": 1, "b": 2}
        assert dumps(obj) == dumps(obj)
