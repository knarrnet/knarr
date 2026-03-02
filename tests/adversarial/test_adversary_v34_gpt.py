"""Adversarial tests for v0.34.0 receipt foundation.

These tests are intentionally strict and are expected to fail against current
implementation bugs. They document undesirable behaviors without fixing code.
"""

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nacl.signing import SigningKey

from knarr.core.messages import MailPullResp, MailSync, TaskResult
from knarr.dht.node import DHTNode
from knarr.dht.storage import Storage
from knarr.mail.sync import SyncEngine


class RecordingBus:
    def __init__(self):
        self.events = []

    def emit(self, event_name: str, **kwargs):
        self.events.append((event_name, kwargs))


class DummyWriter:
    def __init__(self):
        self.close_calls = 0
        self.wait_closed_calls = 0

    def get_extra_info(self, key, default=None):
        if key == "peername":
            return ("127.0.0.1", 10001)
        return default

    def close(self):
        self.close_calls += 1

    async def wait_closed(self):
        self.wait_closed_calls += 1


class ReaderNoBuffer:
    async def read(self, n: int) -> bytes:
        return b"\x00\x00\x00\x10"


class ReaderWithBuffer:
    def __init__(self, first_read: bytes):
        self._first_read = first_read
        self._buffer = bytearray()

    async def read(self, n: int) -> bytes:
        out = self._first_read
        self._first_read = b""
        return out


class CapturingStorage:
    def __init__(self):
        self.receipt_calls = []

    def write_receipt(self, **kwargs):
        self.receipt_calls.append(kwargs)


class RequestTaskStorage:
    def __init__(self):
        self.write_receipt_calls = []

    def insert_task(self, *args, **kwargs):
        return None

    def update_task_status(self, *args, **kwargs):
        return None

    def get_or_create_ledger_entry(self, *args, **kwargs):
        return SimpleNamespace(balance=0.0)

    def update_ledger_consumer(self, *args, **kwargs):
        return None

    def store_receipt(self, *args, **kwargs):
        return None

    def store_credit_note(self, *args, **kwargs):
        return None

    def write_receipt(self, **kwargs):
        self.write_receipt_calls.append(kwargs)


class SyncStorage:
    def __init__(self, store_result=True, pending_outbox=None, peers=None):
        self.store_result = store_result
        self.pending_outbox = pending_outbox or []
        self.peers = peers or []

    def upsert_address(self, *args, **kwargs):
        return None

    def store_mail_from_sync(self, *args, **kwargs):
        return self.store_result

    def upsert_correspondent(self, *args, **kwargs):
        return None

    def count_mail_inbox(self):
        return 0

    def get_pending_outbox(self, *args, **kwargs):
        return self.pending_outbox

    def mark_outbox_sending(self, *args, **kwargs):
        return None

    def mark_outbox_delivered_for_peer(self, *args, **kwargs):
        return None

    def get_peers(self):
        return self.peers

    def get_provider_address(self, *args, **kwargs):
        return None


def make_write_node(with_signing_key: bool) -> DHTNode:
    node = DHTNode.__new__(DHTNode)
    node.node_info = SimpleNamespace(node_id="node-local")
    node.storage = CapturingStorage()
    node._signing_key = SigningKey.generate() if with_signing_key else None
    node._public_key_hex = (
        node._signing_key.verify_key.encode().hex()
        if with_signing_key
        else "f" * 64
    )
    return node


def make_request_task_node(with_signing_key: bool) -> DHTNode:
    node = DHTNode.__new__(DHTNode)
    node.node_info = SimpleNamespace(node_id="node-123", host="127.0.0.1", port=9010)
    node.storage = RequestTaskStorage()
    node._task_events = {}
    node._task_expected_provider = {}
    node._task_results = {}
    node.bus = RecordingBus()
    node.policy = SimpleNamespace(initial_credit=-5.0)
    node._public_key_hex = "b" * 64
    node._signing_key = SigningKey.generate() if with_signing_key else None
    node._sign = lambda msg: msg
    node._get_initial_trust = lambda _nid: 0.5
    node._write_receipt = MagicMock()
    node._sign_receipt = DHTNode._sign_receipt.__get__(node, DHTNode)

    async def _enqueue_write(op, *args):
        out = op(*args)
        if asyncio.iscoroutine(out):
            return await out
        return out

    node._enqueue_write = _enqueue_write
    return node


def make_connection_node(with_bus: bool = True) -> DHTNode:
    node = DHTNode.__new__(DHTNode)
    node._active_connections = 0
    node._plugins = SimpleNamespace(
        on_connect=AsyncMock(return_value=True),
        on_inbound=AsyncMock(return_value=True),
    )
    node.bus = RecordingBus() if with_bus else None
    node.node_info = SimpleNamespace(node_id="provider-node")
    node._peer_last_activity = {}
    node.storage = SimpleNamespace(get_peers=lambda: [])
    node.resolve_peer = lambda node_id, host, port: (host, port)
    node._sync = SimpleNamespace(push_to_peer=AsyncMock())
    node._process_message = AsyncMock(return_value=None)

    async def _enqueue_write_proto(op, *args):
        out = op(*args)
        if asyncio.iscoroutine(out):
            return await out
        return out

    node._enqueue_write_proto = _enqueue_write_proto
    return node


def make_sync_node(storage: SyncStorage, with_receipt_writer: bool):
    node = SimpleNamespace()
    node._config = {"mail": {}}
    node.node_info = SimpleNamespace(node_id="local-node")
    node.storage = storage
    node.bus = RecordingBus()
    node._pool = SimpleNamespace(send=AsyncMock())
    node.resolve_peer = lambda node_id, host, port: (host, port)
    node._sign = lambda msg: msg
    node.receipts = []

    if with_receipt_writer:
        def _write_receipt(**kwargs):
            node.receipts.append(kwargs)
        node._write_receipt = _write_receipt

    async def _enqueue_write(op, *args):
        out = op(*args)
        if asyncio.iscoroutine(out):
            return await out
        return out

    node._enqueue_write = _enqueue_write
    return node


async def run_request_task_once(node: DHTNode):
    resp = TaskResult(
        task_id="remote-task",
        status="completed",
        output_data={"ok": True},
        public_key="a" * 64,
    )
    with patch("knarr.dht.node.request_response", new=AsyncMock(return_value=resp)), patch(
        "knarr.dht.node.verify_message", return_value=True
    ):
        return await DHTNode.request_task(
            node,
            "peer-node",
            "127.0.0.1",
            9030,
            "demo-skill",
            {"x": 1},
            timeout_ms=1000,
            skill_price=1.5,
        )


@pytest.mark.asyncio
async def test_startup_connection_failure_still_closes_writer():
    node = make_connection_node(with_bus=False)
    reader = ReaderNoBuffer()
    writer = DummyWriter()
    with patch("knarr.dht.node.receive_message", new=AsyncMock(return_value=None)):
        await DHTNode._handle_connection(node, reader, writer)
    assert writer.close_calls == 1
    assert writer.wait_closed_calls == 1


@pytest.mark.asyncio
async def test_wiring_invalid_signature_emits_bus_event_for_first_message():
    node = make_connection_node(with_bus=True)
    reader = ReaderWithBuffer(b"\x00\x00\x00\x10")
    writer = DummyWriter()
    fake_msg = SimpleNamespace(type="TASK_REQUEST", public_key="")
    with patch("knarr.dht.node.receive_message", new=AsyncMock(return_value=fake_msg)), patch(
        "knarr.dht.node.verify_message", return_value=False
    ):
        await DHTNode._handle_connection(node, reader, writer)
    names = [name for name, _payload in node.bus.events]
    assert "security.signature_invalid" in names


@pytest.mark.asyncio
async def test_security_http_reject_is_case_insensitive():
    node = make_connection_node(with_bus=True)
    reader = ReaderWithBuffer(b"get ")
    writer = DummyWriter()
    with patch("knarr.dht.node.receive_message", new=AsyncMock(return_value=None)):
        await DHTNode._handle_connection(node, reader, writer)
    names = [name for name, _payload in node.bus.events]
    assert "firewall.blocked" in names


@pytest.mark.asyncio
async def test_wiring_sync_request_task_uses_centralized_write_receipt_helper():
    node = make_request_task_node(with_signing_key=True)
    await run_request_task_once(node)
    assert node._write_receipt.call_count >= 1


@pytest.mark.asyncio
async def test_wiring_missing_mail_receipt_writer_logs_warning():
    storage = SyncStorage(store_result=True)
    node = make_sync_node(storage, with_receipt_writer=False)
    sync = SyncEngine(node)
    sync._log = MagicMock()
    msg = MailSync(
        sender_node_id="peer-1",
        batch_seq=1,
        items=[
            {
                "item_id": "m1",
                "timestamp": time.time(),
                "ttl_expires": time.time() + 3600,
                "msg_type": "plain",
                "body": {"k": "v"},
            }
        ],
    )
    await sync.handle_mail_sync(msg, peer_ip="10.0.0.2")
    assert sync._log.warning.called


@pytest.mark.asyncio
async def test_schema_sync_receipt_payload_has_document_type():
    node = make_request_task_node(with_signing_key=True)
    await run_request_task_once(node)
    payload = json.loads(node.storage.write_receipt_calls[0]["payload_json"])
    assert payload.get("document_type") == "execution_receipt"


@pytest.mark.asyncio
async def test_schema_sync_receipt_signature_is_ed25519_prefixed_hex():
    node = make_request_task_node(with_signing_key=True)
    await run_request_task_once(node)
    sig = node.storage.write_receipt_calls[0]["signature"]
    assert isinstance(sig, str) and sig.startswith("ed25519:")


@pytest.mark.asyncio
async def test_schema_sync_receipt_timestamp_is_millis_utc_z():
    node = make_request_task_node(with_signing_key=True)
    await run_request_task_once(node)
    ts = node.storage.write_receipt_calls[0]["timestamp"]
    assert ts.endswith("Z")
    assert "." in ts


@pytest.mark.asyncio
async def test_schema_sync_receipt_identity_uses_node_id_not_public_key():
    node = make_request_task_node(with_signing_key=True)
    await run_request_task_once(node)
    identity = node.storage.write_receipt_calls[0]["identity"]
    assert identity == node.node_info.node_id


def test_concurrency_receipt_id_collision_not_silently_dropped():
    node = DHTNode.__new__(DHTNode)
    node.node_info = SimpleNamespace(node_id="node-local")
    node.storage = Storage(":memory:")
    node._signing_key = SigningKey.generate()
    node._public_key_hex = node._signing_key.verify_key.encode().hex()

    with patch("secrets.token_hex", return_value="deadbeefcafe"):
        DHTNode._write_receipt(node, "order_ack", {"a": 1}, sign=False)
        DHTNode._write_receipt(node, "order_ack", {"a": 2}, sign=False)

    conn = node.storage._get_conn()
    count = conn.execute("SELECT COUNT(*) FROM receipt_log WHERE document_type = 'order_ack'").fetchone()[0]
    assert count == 2


@pytest.mark.asyncio
async def test_schema_duplicate_mail_receive_receipt_has_payload_hash():
    storage = SyncStorage(store_result=False)
    node = make_sync_node(storage, with_receipt_writer=True)
    sync = SyncEngine(node)
    msg = MailSync(
        sender_node_id="peer-1",
        batch_seq=9,
        items=[
            {
                "item_id": "dup-1",
                "timestamp": time.time(),
                "ttl_expires": time.time() + 100,
                "msg_type": "plain",
                "body": {"x": 1},
            }
        ],
    )
    await sync.handle_mail_sync(msg, peer_ip="10.0.0.9")
    receipt = node.receipts[0]["payload"]["receipt"]
    assert "payload_hash" in receipt


def test_input_write_receipt_does_not_mutate_caller_payload():
    node = make_write_node(with_signing_key=True)
    payload = {"alpha": 1}
    DHTNode._write_receipt(node, "order_ack", payload, sign=False)
    assert payload == {"alpha": 1}


def test_input_write_receipt_handles_non_string_order_ref_without_crash():
    node = make_write_node(with_signing_key=True)
    DHTNode._write_receipt(node, "order_ack", {"x": 1}, order_ref=12345, sign=False)


def test_input_sign_true_requires_non_null_signature():
    node = make_write_node(with_signing_key=False)
    DHTNode._write_receipt(node, "execution_receipt", {"x": 1}, sign=True)
    assert node.storage.receipt_calls[0]["signature"] is not None


def test_input_invalid_proof_purpose_is_rejected():
    node = make_write_node(with_signing_key=True)
    with pytest.raises(ValueError):
        DHTNode._write_receipt(
            node,
            "order_ack",
            {"x": 1},
            proof_purpose="not-a-real-proof-purpose",
            sign=False,
        )


def test_input_nan_amount_is_rejected():
    node = make_write_node(with_signing_key=True)
    with pytest.raises(ValueError):
        DHTNode._write_receipt(node, "credit_note", {"amount": float("nan")}, sign=False)


def test_input_inf_amount_is_rejected():
    node = make_write_node(with_signing_key=True)
    with pytest.raises(ValueError):
        DHTNode._write_receipt(node, "credit_note", {"amount": float("inf")}, sign=False)


@pytest.mark.asyncio
async def test_input_list_body_hash_uses_canonical_json_not_python_repr():
    storage = SyncStorage(store_result=True)
    node = make_sync_node(storage, with_receipt_writer=True)
    sync = SyncEngine(node)
    body = ["a", "b"]
    msg = MailSync(
        sender_node_id="peer-2",
        batch_seq=2,
        items=[
            {
                "item_id": "m-list",
                "timestamp": time.time(),
                "ttl_expires": time.time() + 3600,
                "msg_type": "plain",
                "body": body,
            }
        ],
    )
    await sync.handle_mail_sync(msg, peer_ip="10.0.0.3")
    got_hash = node.receipts[0]["payload"]["receipt"]["payload_hash"]
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    expected_hash = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert got_hash == expected_hash


@pytest.mark.asyncio
async def test_input_self_deliver_zero_body_counts_one_byte():
    pending = [
        {
            "item_id": "self-zero-1",
            "body_json": json.dumps(
                {
                    "item_id": "self-zero-1",
                    "timestamp": time.time(),
                    "ttl_expires": time.time() + 3600,
                    "msg_type": "plain",
                    "body": 0,
                }
            ),
        }
    ]
    storage = SyncStorage(store_result=True, pending_outbox=pending)
    node = make_sync_node(storage, with_receipt_writer=True)
    sync = SyncEngine(node)
    await sync._self_deliver(node.node_info.node_id)
    payload_bytes = node.receipts[0]["payload"]["receipt"]["payload_bytes"]
    assert payload_bytes == 1


@pytest.mark.asyncio
async def test_input_pull_false_body_counts_json_false_bytes():
    peer = SimpleNamespace(node_id="peer-x", host="127.0.0.1", port=9035, version="0.34.0")
    storage = SyncStorage(store_result=True, peers=[peer])
    node = make_sync_node(storage, with_receipt_writer=True)
    pull_items = [
        {
            "item_id": "pull-false-1",
            "timestamp": time.time(),
            "ttl_expires": time.time() + 3000,
            "msg_type": "plain",
            "body": False,
        }
    ]
    node._pool.send = AsyncMock(side_effect=[MailPullResp(sender_node_id="peer-x", items=pull_items), None])
    sync = SyncEngine(node)
    sync._sync_assets_from_mail = AsyncMock(return_value=None)
    await sync._pull_from_peer("peer-x")
    payload_bytes = node.receipts[0]["payload"]["receipt"]["payload_bytes"]
    assert payload_bytes == len("false")


@pytest.mark.asyncio
async def test_concurrency_sync_path_still_writes_receipt_without_signing_key():
    node = make_request_task_node(with_signing_key=False)
    await run_request_task_once(node)
    assert len(node.storage.write_receipt_calls) >= 1
