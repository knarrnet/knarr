"""Adversarial tests for v0.33.0 integration seams.

Goal:
- Verify the S-022 requester mapping patch still behaves correctly.
- Probe refund-flow seams where checks can be bypassed.
- Probe security.* event payloads for potentially sensitive leakage.

These are intentionally adversarial assertions; several tests are expected
to fail on vulnerable implementations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nacl.signing import SigningKey

from knarr.commerce.handlers import make_commerce_handlers
from knarr.commerce.receipts import create_credit_note
from knarr.dashboard.server import CockpitServer
from knarr.dht.node import DHTNode
from knarr.dht.storage import Storage


class CaptureBus:
    def __init__(self):
        self.events = []

    def emit(self, event_type: str, **fields):
        self.events.append({"event": event_type, **fields})

    def first(self, event_type: str):
        for event in self.events:
            if event.get("event") == event_type:
                return event
        return None


class DummyWriter:
    def __init__(self, ip: str):
        self._ip = ip
        self.closed = False

    def get_extra_info(self, key, default=None):
        if key == "peername":
            return (self._ip, 9000)
        return default

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


def _insert_execution_log(storage: Storage, *, task_id: str, caller_node_id):
    conn = storage._get_conn()
    conn.execute(
        """
        INSERT INTO execution_log
            (job_id, skill_name, caller_node_id, status, wall_time_ms, created_at, price, refund_total)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            "echo",
            caller_node_id,
            "completed",
            25,
            time.time(),
            10.0,
            0.0,
        ),
    )
    conn.commit()


def _count_enqueue_calls(mock_node, target_op) -> int:
    count = 0
    for call in mock_node._enqueue_write.call_args_list:
        args = call[0]
        if args and args[0] is target_op:
            count += 1
    return count


def test_exploit_g1_get_execution_log_entry_maps_caller_to_requester():
    """Patch check: caller_node_id should surface as requester_node_id."""
    storage = Storage(":memory:")
    _insert_execution_log(storage, task_id="task-patched", caller_node_id="node-requester-123")

    entry = storage.get_execution_log_entry("task-patched")
    assert entry is not None
    assert entry["requester_node_id"] == "node-requester-123"


def test_exploit_g2_get_execution_log_entry_preserves_null_requester():
    """Patch seam: NULL caller_node_id should stay NULL and not coerce."""
    storage = Storage(":memory:")
    _insert_execution_log(storage, task_id="task-null", caller_node_id=None)

    entry = storage.get_execution_log_entry("task-null")
    assert entry is not None
    assert entry["requester_node_id"] is None


@pytest.mark.asyncio
async def test_exploit_g3_receipt_refund_rejected_when_requester_is_null():
    """Fail-closed check: NULL requester should block refund generation."""
    node = MagicMock()
    node.storage.get_execution_log_entry.return_value = {"price": 10.0, "requester_node_id": None}
    node._enqueue_write = AsyncMock()
    node._sync = MagicMock()
    node._sync.enqueue = AsyncMock()
    handler = make_commerce_handlers(node)["knarr/commerce/receipt"]

    await handler(
        {
            "from_node": "attacker-node",
            "body": {
                "type": "knarr/commerce/receipt",
                "task_id": "task-null",
                "status": "rejected",
                "timestamp": time.time(),
                "refund_requested": True,
            },
        }
    )

    assert node._sync.enqueue.call_count == 0


@pytest.mark.asyncio
async def test_exploit_g4_empty_sender_identity_should_not_pass_refund_verification():
    """Vuln probe: empty sender + empty requester currently passes equality check."""
    node = MagicMock()
    node.storage.get_execution_log_entry.return_value = {"price": 10.0, "requester_node_id": ""}
    node._enqueue_write = AsyncMock()
    node._sync = MagicMock()
    node._sync.enqueue = AsyncMock()
    handler = make_commerce_handlers(node)["knarr/commerce/receipt"]

    await handler(
        {
            "from_node": "",
            "body": {
                "type": "knarr/commerce/receipt",
                "task_id": "task-empty-id",
                "status": "rejected",
                "timestamp": time.time(),
                "refund_requested": True,
            },
        }
    )

    # Security expectation: empty identities should be rejected.
    assert node._sync.enqueue.call_count == 0


@pytest.mark.asyncio
async def test_exploit_g5_credit_note_sender_not_bound_to_original_task_identity():
    """Vuln probe: handle_credit_note lacks sender-to-task identity binding."""
    attacker_pubkey = "ab" * 32
    attacker_node_id = hashlib.sha256(bytes.fromhex(attacker_pubkey)).hexdigest()

    node = MagicMock()
    node._enqueue_write = AsyncMock()
    node.storage.get_execution_log_entry.return_value = {
        "price": 100.0,
        "requester_node_id": "legitimate-requester-node-id",
    }
    node.storage.get_cumulative_refund.return_value = 0.0
    node.storage.get_all_ledger_entries.return_value = [{"peer_public_key": attacker_pubkey}]
    handler = make_commerce_handlers(node)["knarr/commerce/credit_note"]

    await handler(
        {
            "from_node": attacker_node_id,
            "body": {
                "type": "knarr/commerce/credit_note",
                "amount": 40.0,
                "reason": "quality_rejection",
                "timestamp": time.time(),
                "references": {"task_id": "victim-task-id"},
            },
        }
    )

    # Security expectation: sender mismatch to original task identity should reject.
    assert _count_enqueue_calls(node, node.storage.update_ledger_refund) == 0


@pytest.mark.asyncio
async def test_exploit_g6_concurrent_credit_notes_bypass_cumulative_refund_cap():
    """Vuln probe: non-atomic check/update allows >2x cumulative via concurrency."""

    class RaceStorage:
        def __init__(self, pubkey: str):
            self.pubkey = pubkey
            self.refund_total = 0.0

        def get_execution_log_entry(self, task_id: str):
            return {"price": 100.0, "requester_node_id": "victim"}

        def get_cumulative_refund(self, task_id: str):
            return self.refund_total

        def get_all_ledger_entries(self):
            return [{"peer_public_key": self.pubkey}]

        def update_ledger_refund(self, peer_public_key: str, amount: float):
            return None

        def record_refund(self, task_id: str, amount: float):
            self.refund_total += amount
            return None

    class RaceNode:
        def __init__(self, storage):
            self.storage = storage
            self._gate = asyncio.Event()
            self._refund_writes = 0

        async def _enqueue_write(self, op, *args):
            # Hold both update_ledger_refund writes at a barrier so each request
            # can pass the pre-check before either record_refund executes.
            if getattr(op, "__name__", "") == "update_ledger_refund":
                self._refund_writes += 1
                if self._refund_writes >= 2:
                    self._gate.set()
                await asyncio.wait_for(self._gate.wait(), timeout=1.0)
            return op(*args)

    pubkey = "cd" * 32
    sender_node_id = hashlib.sha256(bytes.fromhex(pubkey)).hexdigest()
    node = RaceNode(RaceStorage(pubkey))
    handler = make_commerce_handlers(node)["knarr/commerce/credit_note"]

    item = {
        "from_node": sender_node_id,
        "body": {
            "type": "knarr/commerce/credit_note",
            "amount": 150.0,
            "reason": "partial_refund",
            "timestamp": time.time(),
            "references": {"task_id": "task-race"},
        },
    }

    await asyncio.wait_for(asyncio.gather(handler(dict(item)), handler(dict(item))), timeout=2.0)

    # Security expectation: max 2x original (200.0) should hold under concurrency.
    assert node.storage.refund_total <= 200.0


@pytest.mark.asyncio
async def test_exploit_g7_signed_credit_note_note_type_credit_still_debits_consumer():
    """Vuln probe: note_type is ignored; 'credit' still charges via update_ledger_consumer."""
    provider_sk = SigningKey.generate()
    provider_pubkey = provider_sk.verify_key.encode().hex()
    recipient_pubkey = SigningKey.generate().verify_key.encode().hex()
    sender_node_id = hashlib.sha256(bytes.fromhex(provider_pubkey)).hexdigest()

    credit_note = create_credit_note(
        note_type="credit",
        amount=25.0,
        issuer=provider_pubkey,
        recipient=recipient_pubkey,
        reference="job-credit-note",
        description="refund-like credit note",
        signing_key=provider_sk,
    )

    bus = CaptureBus()
    node = MagicMock(spec=DHTNode)
    node.bus = bus
    node.policy = SimpleNamespace(initial_credit=3.0)
    node._get_initial_trust = MagicMock(return_value=0.3)
    node._check_credit_restored = MagicMock()
    node.storage = MagicMock()
    node.storage.get_async_job.return_value = {
        "job_id": "job-credit-note",
        "provider_node_id": sender_node_id,
        "provider_public_key": provider_pubkey,
        "status": "remote",
    }
    node.storage.get_ledger_entry.return_value = None
    node._enqueue_write = AsyncMock(side_effect=lambda op, *args: op(*args))

    await DHTNode._handle_task_result_mail(
        node,
        {
            "from_node": sender_node_id,
            "body": {"job_id": "job-credit-note", "credit_note": credit_note},
        },
    )

    # Security expectation: note_type='credit' should not debit consumer ledger.
    assert node.storage.update_ledger_consumer.call_count == 0


def test_exploit_g8_security_auth_failed_event_should_not_emit_full_ip_and_endpoint():
    """Leak probe: security.auth_failed currently emits raw source_ip + endpoint."""
    bus = CaptureBus()
    server = MagicMock(spec=CockpitServer)
    server._auth_token = "secret-token"
    server._node = MagicMock()
    server._node.bus = bus

    ok = CockpitServer._check_auth(
        server,
        {"authorization": "Bearer wrong-token"},
        source_ip="203.0.113.9",
        endpoint="/api/upgrade/check",
    )
    assert ok is False

    event = bus.first("security.auth_failed")
    assert event is not None
    # Security expectation: raw network/source details should be redacted.
    assert event["source_ip"] != "203.0.113.9"
    assert event["endpoint"] != "/api/upgrade/check"


@pytest.mark.asyncio
async def test_exploit_g9_security_signature_invalid_event_should_not_emit_full_ip():
    """Leak probe: security.signature_invalid currently emits full peer IP."""
    bus = CaptureBus()
    node = MagicMock(spec=DHTNode)
    node.bus = bus
    node._plugins = MagicMock()
    node._plugins.on_connect = AsyncMock(return_value=True)
    node._active_connections = 0

    fake_msg = SimpleNamespace(type="TaskRequest")
    writer = DummyWriter("198.51.100.44")

    with patch("knarr.dht.node.receive_message", AsyncMock(return_value=fake_msg)), patch(
        "knarr.dht.node.verify_message", return_value=False
    ):
        await DHTNode._handle_connection(node, reader=object(), writer=writer)

    event = bus.first("security.signature_invalid")
    assert event is not None
    # Security expectation: IP should be masked or omitted.
    assert event["from_ip"] != "198.51.100.44"


@pytest.mark.asyncio
async def test_exploit_g10_security_identity_mismatch_event_should_not_emit_full_claimed_id():
    """Leak probe: security.identity_mismatch currently emits full claimed_id."""
    bus = CaptureBus()
    node = MagicMock(spec=DHTNode)
    node.bus = bus
    node._plugins = MagicMock()
    node._plugins.on_connect = AsyncMock(return_value=True)
    node._active_connections = 0

    fake_claimed = "f" * 64
    fake_msg = SimpleNamespace(type="Heartbeat", node_id=fake_claimed, public_key="")
    writer = DummyWriter("203.0.113.50")

    with patch("knarr.dht.node.receive_message", AsyncMock(return_value=fake_msg)), patch(
        "knarr.dht.node.verify_message", return_value=True
    ), patch("knarr.dht.node.verify_node_id", return_value=False):
        await DHTNode._handle_connection(node, reader=object(), writer=writer)

    event = bus.first("security.identity_mismatch")
    assert event is not None
    # Security expectation: full claimed node IDs should not be emitted.
    assert event["claimed_id"] != fake_claimed


@pytest.mark.asyncio
async def test_exploit_g11_security_receipt_forgery_event_should_not_emit_full_issuer():
    """Leak probe: security.receipt_forgery currently emits full issuer pubkey."""
    issuer_pubkey = "e" * 64
    bus = CaptureBus()

    node = MagicMock(spec=DHTNode)
    node.bus = bus
    node.policy = SimpleNamespace(initial_credit=3.0)
    node.storage = MagicMock()
    node.storage.get_async_job.return_value = {
        "job_id": "job-forgery",
        "provider_node_id": "provider-node",
        "provider_public_key": issuer_pubkey,
        "status": "remote",
    }
    node._enqueue_write = AsyncMock(return_value=None)

    forged_note = {
        "type": "credit_note",
        "version": 1,
        "note_type": "debit",
        "amount": 10.0,
        "issuer": issuer_pubkey,
        "recipient": "a" * 64,
        "timestamp": "2026-03-01T00:00:00+00:00",
        "reference": "job-forgery",
        "description": "forged",
        "signature": "invalid",
    }

    with patch("knarr.commerce.receipts.verify_credit_note", return_value=False):
        await DHTNode._handle_task_result_mail(
            node,
            {
                "from_node": "provider-node",
                "body": {"job_id": "job-forgery", "credit_note": json.dumps(forged_note)},
            },
        )

    event = bus.first("security.receipt_forgery")
    assert event is not None
    # Security expectation: issuer key material should be masked in event payload.
    assert event["issuer"] != issuer_pubkey
