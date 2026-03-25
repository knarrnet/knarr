"""ESC-02: Negative price skill announcements — validation tests."""
import math
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from knarr.core.validation import validate_skill_sheet, ValidationError

_BASE = {
    "name": "test-skill",
    "version": "1.0.0",
    "description": "A test skill",
    "tags": ["test"],
    "input_schema": {"query": "string"},
    "output_schema": {"result": "string"},
}


def _sheet(**kwargs):
    d = dict(_BASE)
    d.update(kwargs)
    return d


def test_negative_price_passes_validation():
    """Negative price must not raise ValidationError (ESC-02)."""
    result = validate_skill_sheet(_sheet(price=-1.0))
    assert result.price == -1.0


def test_negative_price_large_bounty():
    """Large negative price (bounty) must pass validation."""
    result = validate_skill_sheet(_sheet(price=-999.99))
    assert result.price == -999.99


def test_nan_price_rejected():
    """NaN price must be rejected."""
    with pytest.raises(ValidationError, match="finite"):
        validate_skill_sheet(_sheet(price=float("nan")))


def test_inf_price_rejected():
    """Positive infinity price must be rejected."""
    with pytest.raises(ValidationError, match="finite"):
        validate_skill_sheet(_sheet(price=float("inf")))


def test_neg_inf_price_rejected():
    """Negative infinity price must be rejected."""
    with pytest.raises(ValidationError, match="finite"):
        validate_skill_sheet(_sheet(price=float("-inf")))


def test_zero_price_valid():
    """Zero price must remain valid."""
    result = validate_skill_sheet(_sheet(price=0))
    assert result.price == 0


def test_positive_price_still_valid():
    """Positive price must still be accepted."""
    result = validate_skill_sheet(_sheet(price=5.0))
    assert result.price == 5.0


def test_price_above_old_max_now_allowed():
    """ESC-02: Hardcoded price ceiling removed. Prices above 1000.0 are now allowed."""
    result = validate_skill_sheet(_sheet(price=1001.0))
    assert result.price == 1001.0
