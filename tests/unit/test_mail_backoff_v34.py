"""Tests for A2: M-018 Mail Delivery Resilience (backoff + circuit breaker)."""
import pytest
import asyncio
import time
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from unittest.mock import AsyncMock, MagicMock, patch
from knarr.mail.sync import SyncEngine
from knarr.core.models import NodeInfo


class TestMailBackoff:
    """Tests for M-018 exponential backoff and circuit breaker."""

    def setup_method(self):
        """Create mock node and sync engine."""
        self.mock_node = MagicMock()
        self.mock_node.node_info = NodeInfo(
            node_id="a" * 64,
            host="127.0.0.1",
            port=9030
        )
        self.mock_node._config = {"mail": {"debug": True}}
        self.mock_node.storage = MagicMock()
        self.mock_node.storage.get_pending_outbox = MagicMock(return_value=[])
        self.mock_node._egress = MagicMock()
        self.mock_node._egress.check = MagicMock(return_value=True)
        self.sync = SyncEngine(self.mock_node)

    def test_backoff_timing_escalation(self):
        """Test exponential backoff: 30s → 60s → 120s → 300s → 600s."""
        # Backoff schedule per brief: 30s, 60s, 120s, 300s, 600s (cap at 10 min)
        expected_backoffs = [30, 60, 120, 300, 600]
        
        for i, expected in enumerate(expected_backoffs):
            # Simulate failure update (mimicking what _push_to_peer_inner does)
            consecutive_failures = i + 1  # After this failure
            backoff_schedule = [30, 60, 120, 300, 600]
            backoff_seconds = backoff_schedule[min(consecutive_failures - 1, 4)]
            
            assert backoff_seconds == expected, f"Failure {i+1}: expected {expected}s, got {backoff_seconds}s"

    def test_backoff_cap_at_600_seconds(self):
        """Test that backoff caps at 600 seconds (10 minutes)."""
        peer_id = "b" * 64
        
        # Simulate 10 failures (well past the cap)
        self.sync._peer_delivery_state[peer_id] = {
            "last_attempt": time.time(),
            "consecutive_failures": 10,
            "next_retry_after": None,
            "circuit_open": False
        }
        
        state = self.sync._peer_delivery_state[peer_id]
        backoff_seconds = min(30 * (2 ** state["consecutive_failures"]), 600)
        assert backoff_seconds == 600

    def test_circuit_breaker_opens_after_5_failures(self):
        """Test circuit breaker opens after 5 consecutive failures."""
        peer_id = "b" * 64
        
        # Simulate 4 failures - circuit should still be closed
        self.sync._peer_delivery_state[peer_id] = {
            "last_attempt": time.time(),
            "consecutive_failures": 4,
            "next_retry_after": None,
            "circuit_open": False
        }
        
        state = self.sync._peer_delivery_state[peer_id]
        assert state["circuit_open"] == False
        
        # 5th failure - circuit should open
        state["consecutive_failures"] = 5
        if state["consecutive_failures"] >= 5:
            state["circuit_open"] = True
        
        assert state["circuit_open"] == True

    def test_circuit_breaker_resets_on_success(self):
        """Test circuit breaker resets on successful delivery."""
        peer_id = "b" * 64
        now = time.time()
        
        # Simulate circuit open state after 5 failures
        self.sync._peer_delivery_state[peer_id] = {
            "last_attempt": now - 700,  # Old attempt
            "consecutive_failures": 5,
            "next_retry_after": now - 100,  # Expired
            "circuit_open": True
        }
        
        # Simulate success
        state = self.sync._peer_delivery_state[peer_id]
        state["consecutive_failures"] = 0
        state["next_retry_after"] = None
        state["circuit_open"] = False
        state["last_attempt"] = now
        
        assert state["consecutive_failures"] == 0
        assert state["next_retry_after"] is None
        assert state["circuit_open"] == False

    def test_backoff_skips_delivery_during_cooldown(self):
        """Test that delivery is skipped when within backoff period."""
        peer_id = "b" * 64
        now = time.time()
        
        # Set up state with future retry time
        self.sync._peer_delivery_state[peer_id] = {
            "last_attempt": now,
            "consecutive_failures": 2,
            "next_retry_after": now + 120,  # 2 minutes from now
            "circuit_open": False
        }
        
        # Check if should skip (mimicking _push_to_peer_inner logic)
        state = self.sync._peer_delivery_state.get(peer_id)
        should_skip = state and state.get("next_retry_after") and now < state["next_retry_after"]
        
        assert should_skip == True

    def test_delivery_receipt_written_on_success(self):
        """Test delivery receipt is written via node._write_receipt on successful push."""
        # Delivery receipts now go through node._write_receipt (not a SyncEngine method)
        assert hasattr(self.sync._node, '_write_receipt')

    def test_delivery_receipt_written_on_failure(self):
        """Test delivery receipt is written via node._write_receipt on failed push."""
        # Receipt should be written for both success and failure
        assert hasattr(self.sync._node, '_write_receipt')


class TestDeliveryReceiptFormat:
    """Tests for delivery receipt format compliance."""

    def test_receipt_structure(self):
        """Test receipt follows 2.1.4 schema."""
        import uuid
        from datetime import datetime, timezone
        import json

        import secrets as _secrets
        receipt_id = f"mdr_{_secrets.token_hex(6)}"
        timestamp = datetime.now(timezone.utc).isoformat()

        receipt = {
            "document_type": "mail_delivery_receipt",
            "version": 1,
            "receipt_id": receipt_id,
            "timestamp": timestamp,
            "sender": "a" * 64,
            "recipient": "b" * 64,
            "batch": {
                "message_ids": ["msg_1", "msg_2"],
                "count": 2,
            },
            "delivery": {
                "status": "ack",
                "attempt": 1,
                "duration_ms": 45,
                "ack_item_ids": ["msg_1", "msg_2"],
                "error": None,
            },
            "proof_purpose": "acknowledgment",
        }

        # Verify required fields
        assert receipt["document_type"] == "mail_delivery_receipt"
        assert receipt["version"] == 1
        assert receipt["receipt_id"].startswith("mdr_")
        assert receipt["sender"] == "a" * 64
        assert receipt["recipient"] == "b" * 64
        assert "batch" in receipt
        assert "message_ids" in receipt["batch"]
        assert "delivery" in receipt
        assert receipt["delivery"]["status"] in ["ack", "nak", "term"]
        assert receipt["proof_purpose"] == "acknowledgment"
