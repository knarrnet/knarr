"""Tests for commerce schemas."""
import time
import pytest
from knarr.commerce.schemas import (
    validate_receipt,
    validate_credit_note,
    validate_settle_request,
    validate_settlement_confirmation,
    validate_tab_reminder,
)

def test_valid_receipt():
    valid, err = validate_receipt({
        "type": "knarr/commerce/receipt",
        "task_id": "test-task",
        "status": "accepted",
        "timestamp": time.time(),
        "quality_rating": 4
    })
    assert valid is True

def test_valid_receipt_rejected():
    valid, err = validate_receipt({
        "type": "knarr/commerce/receipt",
        "task_id": "test-task",
        "status": "rejected",
        "timestamp": time.time(),
        "refund_requested": True
    })
    assert valid is True

def test_invalid_receipt_missing_fields():
    # missing task_id
    valid, err = validate_receipt({
        "type": "knarr/commerce/receipt",
        "status": "accepted",
        "timestamp": time.time(),
    })
    assert valid is False
    assert "required field" in err

def test_invalid_receipt_bad_quality():
    valid, err = validate_receipt({
        "type": "knarr/commerce/receipt",
        "task_id": "test-task",
        "status": "accepted",
        "timestamp": time.time(),
        "quality_rating": 6
    })
    assert valid is False
    assert "quality_rating" in err

def test_valid_credit_note():
    valid, err = validate_credit_note({
        "type": "knarr/commerce/credit_note",
        "amount": 10.5,
        "reason": "quality_rejection",
        "timestamp": time.time(),
        "initiated_by": "provider",
        "references": {"task_id": "T1", "original_amount": 10.5}
    })
    assert valid is True

def test_valid_credit_note_partial():
    valid, err = validate_credit_note({
        "type": "knarr/commerce/credit_note",
        "amount": 5.0,
        "reason": "partial_refund",
        "timestamp": time.time(),
        "references": {"task_id": "T2"}
    })
    assert valid is True

def test_credit_note_missing_references_rejected():
    valid, err = validate_credit_note({
        "type": "knarr/commerce/credit_note",
        "amount": 5.0,
        "reason": "other",
        "timestamp": time.time(),
    })
    assert valid is False
    assert "references" in err

def test_credit_note_missing_task_id_rejected():
    valid, err = validate_credit_note({
        "type": "knarr/commerce/credit_note",
        "amount": 5.0,
        "reason": "other",
        "timestamp": time.time(),
        "references": {"original_amount": 10.0}
    })
    assert valid is False
    assert "task_id" in err

def test_valid_settle_request():
    valid, err = validate_settle_request({
        "type": "knarr/commerce/settle_request",
        "current_balance": -100.0,
        "credit_limit": 200.0,
        "provider_wallet": "A" * 44,
        "timestamp": time.time(),
        "requested_action": "settle_to_zero"
    })
    assert valid is True

def test_valid_settlement_confirmation():
    valid, err = validate_settlement_confirmation({
        "type": "knarr/commerce/settlement_confirmation",
        "tx_hash": "A" * 88,
        "amount_settled": 100.0,
        "timestamp": time.time()
    })
    assert valid is True

def test_valid_tab_reminder():
    valid, err = validate_tab_reminder({
        "type": "knarr/commerce/tab_reminder",
        "current_balance": -180.0,
        "credit_limit": 200.0,
        "utilization_pct": 90.0,
        "timestamp": time.time()
    })
    assert valid is True
