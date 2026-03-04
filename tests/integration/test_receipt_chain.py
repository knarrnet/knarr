"""Integration tests: End-to-end receipt chain verification.

Verifies that the full receipt chain for a task execution is intact:
  order_ack → order_executing → execution_receipt → credit_note

Also verifies the mail layer chain:
  mail_delivery_receipt → mail_receive_receipt

These tests exercise the proposed-c storage stub directly with realistic
payloads that mirror what the patched node.py and sync.py would produce.
"""
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Optional
import pytest

import sys
import os

_PROPOSED_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src"))
_BASE_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../src"))
if _PROPOSED_SRC not in sys.path:
    sys.path.insert(0, _PROPOSED_SRC)
if _BASE_SRC not in sys.path:
    sys.path.insert(1, _BASE_SRC)

from knarr.dht.storage import StorageStub


# -----------------------------------------------------------------------
# FakeNode (same helper as unit tests)
# -----------------------------------------------------------------------

class FakeNode:
    def __init__(self):
        from nacl.signing import SigningKey
        self._debug = False
        self.storage = StorageStub(":memory:")
        self._signing_key = SigningKey.generate()
        self._public_key_hex = self._signing_key.verify_key.encode().hex()

    def _write_receipt(
        self,
        document_type: str,
        payload: dict,
        counterparty: Optional[str] = None,
        order_ref: Optional[str] = None,
        proof_purpose: str = "assertion",
        sign: bool = False,
    ) -> str:
        import secrets as _secrets
        from datetime import datetime, timezone as _tz

        _prefix_map = {
            "execution_receipt": "exec",
            "credit_note": "cn",
            "mail_delivery_receipt": "mdr",
            "mail_receive_receipt": "mrr",
            "order_ack": "oack",
            "order_executing": "oexe",
        }
        type_prefix = _prefix_map.get(document_type, "rct")
        receipt_id = f"{type_prefix}_{_secrets.token_hex(6)}"

        _now = datetime.now(_tz.utc)
        timestamp = _now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_now.microsecond // 1000:03d}Z"

        payload["document_type"] = document_type
        payload["version"] = 1
        payload["receipt_id"] = receipt_id
        payload["timestamp"] = timestamp
        if sign:
            payload["cryptosuite"] = "ed25519-jcs"
        payload["proof_purpose"] = proof_purpose

        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))

        signature: Optional[str] = None
        if sign and self._signing_key:
            raw_sig = self._signing_key.sign(payload_json.encode("utf-8")).signature
            signature = "ed25519:" + raw_sig.hex()

        try:
            self.storage.write_receipt(
                receipt_id=receipt_id,
                document_type=document_type,
                timestamp=timestamp,
                identity=self._public_key_hex,
                counterparty=counterparty,
                order_ref=order_ref,
                proof_purpose=proof_purpose,
                payload_json=payload_json,
                signature=signature,
            )
        except Exception:
            pass

        return receipt_id


# -----------------------------------------------------------------------
# Simulate the full task execution receipt chain
# -----------------------------------------------------------------------

def simulate_task_execution(node: FakeNode, job_id: str, caller_id: str,
                              skill: str, price: float) -> dict:
    """
    Simulate the receipt writes that the patched node.py would produce
    for one successful task execution. Returns a dict mapping
    document_type -> receipt_id for chain verification.
    """
    chain = {}

    # 1. order_ack — task accepted into queue (Patch 8 or 9)
    chain["order_ack"] = node._write_receipt(
        document_type="order_ack",
        payload={
            "provider": node._public_key_hex,
            "caller": caller_id,
            "skill_uri": f"knarr:///{skill}",
            "queue": {"position": 1, "estimated_wait_ms": None},
        },
        counterparty=None,
        order_ref=job_id,
        proof_purpose="assertion",
        sign=False,
    )

    # Simulate brief queue wait
    queue_wait_ms = 8

    # 2. order_executing — task dequeued, handler starting (Patch 2)
    chain["order_executing"] = node._write_receipt(
        document_type="order_executing",
        payload={
            "provider": node._public_key_hex,
            "caller": caller_id,
            "skill_uri": f"knarr:///{skill}",
            "queue_wait_ms": queue_wait_ms,
        },
        counterparty=None,
        order_ref=job_id,
        proof_purpose="assertion",
        sign=False,
    )

    # Simulate handler execution
    input_hash = hashlib.sha256(b'{"query":"hello"}').hexdigest()
    output_data = {"result": "world", "tokens": 42}
    output_hash = hashlib.sha256(
        json.dumps(output_data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    wall_ms = 1200

    # 3. execution_receipt — billable success (Patch 3)
    chain["execution_receipt"] = node._write_receipt(
        document_type="execution_receipt",
        payload={
            "provider": node._public_key_hex,
            "caller": caller_id,
            "skill_uri": f"knarr:///{skill}",
            "execution": {
                "status": "completed",
                "duration_ms": wall_ms,
                "input_hash": f"sha256:{input_hash}",
                "output_hash": f"sha256:{output_hash}",
                "error": None,
            },
            "settlement": {
                "credit_note_ref": None,
                "amount": float(price),
                "currency": "credits",
            },
        },
        counterparty=caller_id,
        order_ref=job_id,
        proof_purpose="assertion",
        sign=True,
    )

    # 4. credit_note — after store_credit_note (Patch 4)
    chain["credit_note"] = node._write_receipt(
        document_type="credit_note",
        payload={
            "note_type": "debit",
            "amount": float(price),
            "currency": "credits",
            "issuer": node._public_key_hex,
            "recipient": "pub_" + caller_id,
            "reference": job_id,
            "description": f"skill:{skill} execution",
        },
        counterparty=caller_id,
        order_ref=job_id,
        proof_purpose="assertion",
        sign=True,
    )

    return chain


def simulate_mail_delivery(sender_node: FakeNode, receiver_node: FakeNode,
                            message_ids: list) -> dict:
    """
    Simulate the receipt writes for one mail push cycle.
    Returns chain dict: delivery_receipt, receive_receipts list.
    """
    chain = {}

    # Sender writes mail_delivery_receipt (ack)
    chain["mail_delivery_receipt"] = sender_node._write_receipt(
        document_type="mail_delivery_receipt",
        payload={
            "sender": sender_node._public_key_hex,
            "recipient": receiver_node._public_key_hex,
            "batch": {
                "message_ids": message_ids,
                "message_count": len(message_ids),
            },
            "delivery": {
                "status": "ack",
                "attempt": 1,
                "endpoint": f"{receiver_node._public_key_hex[:8]}@tcp",
                "duration_ms": 45,
                "ack_item_ids": message_ids,
            },
        },
        counterparty=receiver_node._public_key_hex,
        order_ref=message_ids[0] if message_ids else None,
        proof_purpose="acknowledgment",
        sign=True,
    )

    # Receiver writes mail_receive_receipt for each message
    chain["mail_receive_receipts"] = []
    for msg_id in message_ids:
        body_str = json.dumps({"msg_id": msg_id, "text": "hello"})
        payload_hash = "sha256:" + hashlib.sha256(body_str.encode()).hexdigest()
        rid = receiver_node._write_receipt(
            document_type="mail_receive_receipt",
            payload={
                "receiver": receiver_node._public_key_hex,
                "sender": sender_node._public_key_hex,
                "message_id": msg_id,
                "message_type": "text",
                "receipt": {
                    "status": "stored",
                    "payload_bytes": len(body_str.encode()),
                    "payload_hash": payload_hash,
                },
            },
            counterparty=sender_node._public_key_hex,
            order_ref=msg_id,
            proof_purpose="acknowledgment",
            sign=True,
        )
        chain["mail_receive_receipts"].append(rid)

    return chain


# -----------------------------------------------------------------------
# Tests: Task execution chain
# -----------------------------------------------------------------------

class TestTaskExecutionChain:
    """Verify the 4-link chain for a successful task execution."""

    def test_chain_produces_four_receipt_types(self):
        node = FakeNode()
        chain = simulate_task_execution(
            node, job_id="job-chain-001", caller_id="c" * 64,
            skill="llm/chat", price=2.0
        )
        assert "order_ack" in chain
        assert "order_executing" in chain
        assert "execution_receipt" in chain
        assert "credit_note" in chain
        assert node.storage.count_receipts() == 4

    def test_order_ack_written_before_executing(self):
        node = FakeNode()
        chain = simulate_task_execution(
            node, job_id="job-chain-002", caller_id="c" * 64,
            skill="llm/chat", price=1.0
        )
        oack = node.storage.get_receipt(chain["order_ack"])
        oexe = node.storage.get_receipt(chain["order_executing"])
        # Both have timestamps — oack must be earlier or equal
        assert oack["created_at"] <= oexe["created_at"]

    def test_order_ack_is_unsigned(self):
        node = FakeNode()
        chain = simulate_task_execution(
            node, job_id="job-chain-003", caller_id="c" * 64,
            skill="embed", price=0.5
        )
        row = node.storage.get_receipt(chain["order_ack"])
        assert row["signature"] is None

    def test_order_executing_is_unsigned(self):
        node = FakeNode()
        chain = simulate_task_execution(
            node, job_id="job-chain-004", caller_id="c" * 64,
            skill="embed", price=0.5
        )
        row = node.storage.get_receipt(chain["order_executing"])
        assert row["signature"] is None

    def test_execution_receipt_is_signed(self):
        node = FakeNode()
        chain = simulate_task_execution(
            node, job_id="job-chain-005", caller_id="c" * 64,
            skill="llm/chat", price=3.0
        )
        row = node.storage.get_receipt(chain["execution_receipt"])
        assert row["signature"] is not None
        assert row["signature"].startswith("ed25519:")

    def test_credit_note_is_signed(self):
        node = FakeNode()
        chain = simulate_task_execution(
            node, job_id="job-chain-006", caller_id="c" * 64,
            skill="llm/chat", price=3.0
        )
        row = node.storage.get_receipt(chain["credit_note"])
        assert row["signature"] is not None

    def test_all_chain_links_reference_same_order_ref(self):
        node = FakeNode()
        job_id = "job-chain-007"
        chain = simulate_task_execution(
            node, job_id=job_id, caller_id="c" * 64,
            skill="llm/chat", price=2.0
        )
        for doc_type, rid in chain.items():
            row = node.storage.get_receipt(rid)
            assert row["order_ref"] == job_id, (
                f"{doc_type} has wrong order_ref: {row['order_ref']!r}"
            )

    def test_execution_receipt_status_completed(self):
        node = FakeNode()
        chain = simulate_task_execution(
            node, job_id="job-chain-008", caller_id="c" * 64,
            skill="llm/chat", price=5.0
        )
        row = node.storage.get_receipt(chain["execution_receipt"])
        p = json.loads(row["payload_json"])
        assert p["execution"]["status"] == "completed"
        assert p["settlement"]["amount"] == 5.0

    def test_credit_note_amount_matches_execution(self):
        node = FakeNode()
        price = 7.5
        chain = simulate_task_execution(
            node, job_id="job-chain-009", caller_id="c" * 64,
            skill="llm/chat", price=price
        )
        exec_row = node.storage.get_receipt(chain["execution_receipt"])
        cn_row = node.storage.get_receipt(chain["credit_note"])
        ep = json.loads(exec_row["payload_json"])
        cp = json.loads(cn_row["payload_json"])
        assert ep["settlement"]["amount"] == cp["amount"] == price

    def test_multiple_task_chains_isolated(self):
        """Ten independent task chains must produce 40 receipts total."""
        node = FakeNode()
        for i in range(10):
            simulate_task_execution(
                node, job_id=f"job-multi-{i:03d}", caller_id="c" * 64,
                skill="web-search", price=float(i + 1)
            )
        assert node.storage.count_receipts() == 40
        assert node.storage.count_receipts("order_ack") == 10
        assert node.storage.count_receipts("order_executing") == 10
        assert node.storage.count_receipts("execution_receipt") == 10
        assert node.storage.count_receipts("credit_note") == 10

    def test_failed_task_has_no_credit_note(self):
        """Failed tasks: execution_receipt + order_ack/executing, but NO credit_note."""
        node = FakeNode()
        job_id = "job-fail-chain-001"
        caller_id = "d" * 64

        # order_ack
        node._write_receipt(
            "order_ack",
            {"provider": node._public_key_hex, "caller": caller_id,
             "skill_uri": "knarr:///llm/chat", "queue": {"position": 1, "estimated_wait_ms": None}},
            order_ref=job_id, sign=False,
        )

        # order_executing
        node._write_receipt(
            "order_executing",
            {"provider": node._public_key_hex, "caller": caller_id,
             "skill_uri": "knarr:///llm/chat", "queue_wait_ms": 5},
            order_ref=job_id, sign=False,
        )

        # execution_receipt (failed)
        node._write_receipt(
            "execution_receipt",
            {
                "provider": node._public_key_hex,
                "caller": caller_id,
                "skill_uri": "knarr:///llm/chat",
                "execution": {"status": "failed", "duration_ms": 500,
                               "input_hash": None, "output_hash": None, "error": "timeout"},
                "settlement": {"credit_note_ref": None, "amount": 0.0, "currency": "credits"},
            },
            counterparty=caller_id,
            order_ref=job_id,
            proof_purpose="assertion",
            sign=True,
        )

        # No credit_note for failed tasks
        assert node.storage.count_receipts("order_ack") == 1
        assert node.storage.count_receipts("order_executing") == 1
        assert node.storage.count_receipts("execution_receipt") == 1
        assert node.storage.count_receipts("credit_note") == 0
        assert node.storage.count_receipts() == 3


# -----------------------------------------------------------------------
# Tests: Mail delivery chain
# -----------------------------------------------------------------------

class TestMailDeliveryChain:
    """Verify the mail delivery + receive receipt chain."""

    def test_sender_writes_delivery_receipt(self):
        sender = FakeNode()
        receiver = FakeNode()
        chain = simulate_mail_delivery(sender, receiver, ["msg-001", "msg-002"])
        assert sender.storage.count_receipts("mail_delivery_receipt") == 1

    def test_receiver_writes_receive_receipt_per_message(self):
        sender = FakeNode()
        receiver = FakeNode()
        messages = ["msg-001", "msg-002", "msg-003"]
        chain = simulate_mail_delivery(sender, receiver, messages)
        assert receiver.storage.count_receipts("mail_receive_receipt") == 3

    def test_delivery_receipt_has_ack_status(self):
        sender = FakeNode()
        receiver = FakeNode()
        chain = simulate_mail_delivery(sender, receiver, ["msg-001"])
        row = sender.storage.get_receipt(chain["mail_delivery_receipt"])
        p = json.loads(row["payload_json"])
        assert p["delivery"]["status"] == "ack"

    def test_receive_receipts_have_stored_status(self):
        sender = FakeNode()
        receiver = FakeNode()
        chain = simulate_mail_delivery(sender, receiver, ["msg-001", "msg-002"])
        for rid in chain["mail_receive_receipts"]:
            row = receiver.storage.get_receipt(rid)
            p = json.loads(row["payload_json"])
            assert p["receipt"]["status"] == "stored"

    def test_delivery_receipt_proof_purpose_acknowledgment(self):
        sender = FakeNode()
        receiver = FakeNode()
        chain = simulate_mail_delivery(sender, receiver, ["msg-001"])
        row = sender.storage.get_receipt(chain["mail_delivery_receipt"])
        assert row["proof_purpose"] == "acknowledgment"

    def test_receive_receipt_proof_purpose_acknowledgment(self):
        sender = FakeNode()
        receiver = FakeNode()
        chain = simulate_mail_delivery(sender, receiver, ["msg-001"])
        rid = chain["mail_receive_receipts"][0]
        row = receiver.storage.get_receipt(rid)
        assert row["proof_purpose"] == "acknowledgment"

    def test_delivery_receipt_is_signed_by_sender(self):
        sender = FakeNode()
        receiver = FakeNode()
        chain = simulate_mail_delivery(sender, receiver, ["msg-001"])
        row = sender.storage.get_receipt(chain["mail_delivery_receipt"])
        assert row["signature"].startswith("ed25519:")
        assert row["identity"] == sender._public_key_hex

    def test_receive_receipt_is_signed_by_receiver(self):
        sender = FakeNode()
        receiver = FakeNode()
        chain = simulate_mail_delivery(sender, receiver, ["msg-001"])
        rid = chain["mail_receive_receipts"][0]
        row = receiver.storage.get_receipt(rid)
        assert row["signature"].startswith("ed25519:")
        assert row["identity"] == receiver._public_key_hex

    def test_receive_receipt_payload_hash_is_sha256(self):
        sender = FakeNode()
        receiver = FakeNode()
        chain = simulate_mail_delivery(sender, receiver, ["msg-001"])
        rid = chain["mail_receive_receipts"][0]
        row = receiver.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["receipt"]["payload_hash"].startswith("sha256:")

    def test_delivery_and_receive_receipts_independent_storage(self):
        """Sender and receiver each maintain their own receipt_log."""
        sender = FakeNode()
        receiver = FakeNode()
        chain = simulate_mail_delivery(sender, receiver, ["msg-001", "msg-002"])
        # Sender has 1 delivery receipt
        assert sender.storage.count_receipts() == 1
        # Receiver has 2 receive receipts
        assert receiver.storage.count_receipts() == 2

    def test_delivery_receipt_batch_count_matches_messages(self):
        sender = FakeNode()
        receiver = FakeNode()
        messages = ["msg-001", "msg-002", "msg-003", "msg-004", "msg-005"]
        chain = simulate_mail_delivery(sender, receiver, messages)
        row = sender.storage.get_receipt(chain["mail_delivery_receipt"])
        p = json.loads(row["payload_json"])
        assert p["batch"]["message_count"] == 5
        assert len(p["batch"]["message_ids"]) == 5

    def test_delivery_receipt_nak_shape(self):
        """Verify nak delivery receipt shape (failed push)."""
        sender = FakeNode()
        item_ids = ["msg-fail-001", "msg-fail-002"]
        rid = sender._write_receipt(
            document_type="mail_delivery_receipt",
            payload={
                "sender": sender._public_key_hex,
                "recipient": "f" * 64,
                "batch": {"message_ids": item_ids, "message_count": len(item_ids)},
                "delivery": {
                    "status": "nak",
                    "attempt": 3,
                    "endpoint": "ffffffff@tcp",
                    "duration_ms": 10000,
                    "error": "tcp_timeout",
                },
            },
            counterparty="f" * 64,
            order_ref=item_ids[0],
            proof_purpose="acknowledgment",
            sign=True,
        )
        row = sender.storage.get_receipt(rid)
        p = json.loads(row["payload_json"])
        assert p["delivery"]["status"] == "nak"
        assert p["delivery"]["error"] == "tcp_timeout"
        assert p["delivery"]["attempt"] == 3


# -----------------------------------------------------------------------
# Tests: Signature chain integrity
# -----------------------------------------------------------------------

class TestReceiptChainSignatureIntegrity:
    """All signed receipts in a chain can be verified against the issuing node."""

    def _verify_sig(self, node_pubkey_hex: str, payload_json: str, signature: str) -> bool:
        from nacl.signing import VerifyKey
        from nacl.exceptions import BadSignatureError
        try:
            sig_hex = signature.split(":", 1)[1]
            vk = VerifyKey(bytes.fromhex(node_pubkey_hex))
            vk.verify(payload_json.encode("utf-8"), bytes.fromhex(sig_hex))
            return True
        except (BadSignatureError, Exception):
            return False

    def test_all_signed_receipts_in_task_chain_verify(self):
        node = FakeNode()
        chain = simulate_task_execution(
            node, "job-sigchain-001", "c" * 64, "llm/chat", 2.0
        )
        signed_types = ["execution_receipt", "credit_note"]
        for doc_type in signed_types:
            rid = chain[doc_type]
            row = node.storage.get_receipt(rid)
            assert row["signature"] is not None
            ok = self._verify_sig(node._public_key_hex, row["payload_json"], row["signature"])
            assert ok, f"Signature invalid for {doc_type} receipt {rid}"

    def test_all_signed_receipts_in_mail_chain_verify(self):
        sender = FakeNode()
        receiver = FakeNode()
        chain = simulate_mail_delivery(sender, receiver, ["msg-001"])

        # Sender's delivery receipt
        dr_row = sender.storage.get_receipt(chain["mail_delivery_receipt"])
        ok = self._verify_sig(sender._public_key_hex, dr_row["payload_json"], dr_row["signature"])
        assert ok, "Mail delivery receipt signature invalid"

        # Receiver's receive receipt
        rr_rid = chain["mail_receive_receipts"][0]
        rr_row = receiver.storage.get_receipt(rr_rid)
        ok = self._verify_sig(receiver._public_key_hex, rr_row["payload_json"], rr_row["signature"])
        assert ok, "Mail receive receipt signature invalid"
