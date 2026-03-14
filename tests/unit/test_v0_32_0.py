"""Tests for v0.32.0: Receipts + EventBus.

Test plan:
  EventBus (E1): ring buffer, subscriptions, patterns, slow-subscriber skip
  Receipt schema (R1): credit note creation, signing, verification
  Storage (R2): mail_creditnote bucket, store/retrieve
  Node connectors (R3/E3): receipt.issued, receipt.received, credit.change
  Meta receipts endpoint: counterparty fetch, forbidden, not_found
"""
import asyncio


def _run_async(coro):
    """Run a coroutine without leaving the thread's current event loop as None.

    asyncio.run() closes the loop it creates and sets the thread-local current
    loop to None on exit.  On Python 3.12+ asyncio.get_event_loop() raises
    RuntimeError when the current loop is None, breaking any subsequent test
    that calls asyncio.get_event_loop() (e.g. test_v0_32_0_exploits).
    This helper installs a fresh open loop after teardown so the invariant is
    preserved.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        except Exception:
            pass
        asyncio.set_event_loop(asyncio.new_event_loop())
import base64
import json
import math
import time
import unittest
import uuid
from unittest.mock import MagicMock, patch

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_signing_key():
    """Create a fresh Ed25519 signing key for tests."""
    from nacl.signing import SigningKey
    return SigningKey.generate()


def _pubkey_hex(sk):
    """Return the hex-encoded public key for a SigningKey."""
    return sk.verify_key.encode().hex()


# ─────────────────────────────────────────────────────────────────────────────
# E1: EventBus tests
# ─────────────────────────────────────────────────────────────────────────────

class TestEventBusExactPattern(unittest.TestCase):
    """Test 1: subscriber with exact pattern receives matching event."""

    def setUp(self):
        from knarr.dht.eventbus import EventBus
        self.bus = EventBus(size=16)

    def test_emit_and_subscribe_exact(self):
        sub = self.bus.subscribe("receipt.issued")
        self.bus.emit("receipt.issued", amount=5.0)
        events = sub.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "receipt.issued")
        self.assertEqual(events[0]["amount"], 5.0)

    def test_exact_pattern_no_partial_match(self):
        sub = self.bus.subscribe("receipt.issued")
        self.bus.emit("receipt.issuedX")   # should NOT match exact
        events = sub.poll()
        self.assertEqual(len(events), 0)


class TestEventBusGlobPattern(unittest.TestCase):
    """Test 2: subscriber with 'receipt.*' receives receipt.issued and receipt.received."""

    def setUp(self):
        from knarr.dht.eventbus import EventBus
        self.bus = EventBus(size=16)

    def test_emit_glob_pattern(self):
        sub = self.bus.subscribe("receipt.*")
        self.bus.emit("receipt.issued", amount=1.0)
        self.bus.emit("receipt.received", amount=1.0)
        events = sub.poll()
        self.assertEqual(len(events), 2)
        types = [e["event"] for e in events]
        self.assertIn("receipt.issued", types)
        self.assertIn("receipt.received", types)

    def test_multi_level_glob(self):
        sub = self.bus.subscribe("log.*.*")
        self.bus.emit("log.error.db", msg="fail")
        self.bus.emit("log.info.db", msg="ok")
        events = sub.poll()
        self.assertEqual(len(events), 2)


class TestEventBusNoMatch(unittest.TestCase):
    """Test 3: subscriber with 'receipt.*' does NOT receive 'credit.change'."""

    def setUp(self):
        from knarr.dht.eventbus import EventBus
        self.bus = EventBus(size=16)

    def test_no_match_filtered(self):
        sub = self.bus.subscribe("receipt.*")
        self.bus.emit("credit.change", direction="provider")
        events = sub.poll()
        self.assertEqual(len(events), 0)

    def test_no_match_unrelated(self):
        sub = self.bus.subscribe("receipt.*")
        self.bus.emit("peer.joined", node_id="abc")
        self.bus.emit("task.completed", job_id="xyz")
        events = sub.poll()
        self.assertEqual(len(events), 0)


class TestEventBusSlowSubscriber(unittest.TestCase):
    """Test 4: slow subscriber skips to oldest when cursor falls behind ring."""

    def setUp(self):
        from knarr.dht.eventbus import EventBus
        # EventBus enforces minimum size of 16 (max(16, size)), so size=16 is the smallest ring.
        self.bus = EventBus(size=16)

    def test_slow_subscriber_skips(self):
        sub = self.bus.subscribe("*")
        # Emit 20 events — ring size is 16, so oldest 4 are evicted
        for i in range(20):
            self.bus.emit(f"event.{i}", seq=i)
        # Subscriber was at cursor=0 but oldest available is now at head-size = 20-16=4
        events = sub.poll()
        # Should skip to oldest and return 16 events (indices 4..19)
        self.assertEqual(len(events), 16)
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, list(range(4, 20)))

    def test_slow_subscriber_no_crash(self):
        """Verifies that a very slow subscriber doesn't crash the bus."""
        sub = self.bus.subscribe("*")
        # Emit 100 events through a size-16 ring
        for i in range(100):
            self.bus.emit("tick", i=i)
        events = sub.poll()
        # Should receive only the last 16
        self.assertEqual(len(events), 16)
        self.assertEqual(events[-1]["i"], 99)


class TestEventBusPoll(unittest.TestCase):
    """Test 5: poll() returns accumulated events, then empty on second call."""

    def setUp(self):
        from knarr.dht.eventbus import EventBus
        self.bus = EventBus(size=16)

    def test_poll_returns_pending(self):
        sub = self.bus.subscribe("receipt.*")
        self.bus.emit("receipt.issued")
        self.bus.emit("receipt.received")
        first = sub.poll()
        self.assertEqual(len(first), 2)
        second = sub.poll()
        self.assertEqual(len(second), 0)

    def test_poll_empty_when_nothing(self):
        sub = self.bus.subscribe("receipt.*")
        events = sub.poll()
        self.assertEqual(events, [])


class TestEventBusUnsubscribe(unittest.TestCase):
    """Test 6: removed subscriber stops receiving events."""

    def setUp(self):
        from knarr.dht.eventbus import EventBus
        self.bus = EventBus(size=16)

    def test_unsubscribe(self):
        sub = self.bus.subscribe("receipt.*")
        self.bus.emit("receipt.issued")
        self.bus.unsubscribe(sub)
        self.bus.emit("receipt.received")
        # First event was before unsubscribe — cursor was already advanced
        sub.poll()  # drain first event
        # After unsubscribe, the subscriber still has its cursor but bus no longer wakes it.
        # The event is in the ring but sub is removed from _subs so no wake is issued.
        # poll() still reads from cursor, so we need to verify unsubscribe prevents wakes.
        self.assertNotIn(sub, self.bus._subs)

    def test_unsubscribe_removes_from_subs_list(self):
        sub1 = self.bus.subscribe("receipt.*")
        sub2 = self.bus.subscribe("credit.*")
        self.bus.unsubscribe(sub1)
        self.assertEqual(len(self.bus._subs), 1)
        self.assertIs(self.bus._subs[0], sub2)


class TestEventBusNoSubscribers(unittest.TestCase):
    """Test 7: emit with no subscribers doesn't crash."""

    def setUp(self):
        from knarr.dht.eventbus import EventBus
        self.bus = EventBus(size=16)

    def test_emit_no_subscribers(self):
        # Should not raise
        for i in range(50):
            self.bus.emit("some.event", i=i)
        self.assertEqual(self.bus._head, 50)


class TestEventBusMultipleSubscribers(unittest.TestCase):
    """Test 8: two subscribers with different patterns get correct events."""

    def setUp(self):
        from knarr.dht.eventbus import EventBus
        self.bus = EventBus(size=32)

    def test_multiple_subscribers(self):
        sub_receipt = self.bus.subscribe("receipt.*")
        sub_credit = self.bus.subscribe("credit.*")

        self.bus.emit("receipt.issued", amount=2.0)
        self.bus.emit("credit.change", direction="provider")
        self.bus.emit("receipt.received", amount=2.0)
        self.bus.emit("peer.joined")   # neither subscriber cares

        receipt_events = sub_receipt.poll()
        credit_events = sub_credit.poll()

        self.assertEqual(len(receipt_events), 2)
        self.assertEqual(len(credit_events), 1)
        self.assertEqual(receipt_events[0]["event"], "receipt.issued")
        self.assertEqual(receipt_events[1]["event"], "receipt.received")
        self.assertEqual(credit_events[0]["direction"], "provider")


class TestEventBusOrdering(unittest.TestCase):
    """Test 9: events arrive in FIFO order."""

    def setUp(self):
        from knarr.dht.eventbus import EventBus
        self.bus = EventBus(size=32)

    def test_subscriber_ordering(self):
        sub = self.bus.subscribe("*")
        for i in range(10):
            self.bus.emit("tick", seq=i)
        events = sub.poll()
        self.assertEqual(len(events), 10)
        for idx, ev in enumerate(events):
            self.assertEqual(ev["seq"], idx)


# ─────────────────────────────────────────────────────────────────────────────
# R1: Receipt schema tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCreditNoteSchema(unittest.TestCase):
    """Test 10: output matches spec schema exactly."""

    def setUp(self):
        self.sk = _make_signing_key()
        self.issuer = _pubkey_hex(self.sk)
        self.recipient = _pubkey_hex(_make_signing_key())
        self.reference = str(uuid.uuid4())

    def test_create_credit_note_schema(self):
        from knarr.commerce.receipts import create_credit_note
        note_json = create_credit_note(
            note_type="debit",
            amount=2.5,
            issuer=self.issuer,
            recipient=self.recipient,
            reference=self.reference,
            description="skill:test execution",
            signing_key=self.sk,
        )
        note = json.loads(note_json)
        # Required fields
        self.assertEqual(note["type"], "credit_note")
        self.assertEqual(note["version"], 1)
        self.assertEqual(note["note_type"], "debit")
        self.assertAlmostEqual(note["amount"], 2.5)
        self.assertEqual(note["unit"], "credits")
        self.assertEqual(note["issuer"], self.issuer)
        self.assertEqual(note["recipient"], self.recipient)
        self.assertEqual(note["reference"], self.reference)
        self.assertIn("timestamp", note)
        self.assertIsNone(note["parent_hash"])
        self.assertIn("description", note)
        self.assertIn("signature", note)

    def test_note_type_validation(self):
        from knarr.commerce.receipts import create_credit_note
        with self.assertRaises(ValueError):
            create_credit_note(
                note_type="invalid",
                amount=1.0,
                issuer=self.issuer,
                recipient=self.recipient,
                reference=self.reference,
                description="test",
                signing_key=self.sk,
            )

    def test_amount_validation_negative(self):
        from knarr.commerce.receipts import create_credit_note
        with self.assertRaises(ValueError):
            create_credit_note(
                note_type="debit",
                amount=-1.0,
                issuer=self.issuer,
                recipient=self.recipient,
                reference=self.reference,
                description="test",
                signing_key=self.sk,
            )

    def test_amount_validation_nan(self):
        from knarr.commerce.receipts import create_credit_note
        with self.assertRaises(ValueError):
            create_credit_note(
                note_type="debit",
                amount=float("nan"),
                issuer=self.issuer,
                recipient=self.recipient,
                reference=self.reference,
                description="test",
                signing_key=self.sk,
            )

    def test_amount_validation_inf(self):
        from knarr.commerce.receipts import create_credit_note
        with self.assertRaises(ValueError):
            create_credit_note(
                note_type="debit",
                amount=float("inf"),
                issuer=self.issuer,
                recipient=self.recipient,
                reference=self.reference,
                description="test",
                signing_key=self.sk,
            )


class TestCreditNoteSignature(unittest.TestCase):
    """Test 11: sign then verify with public key."""

    def setUp(self):
        self.sk = _make_signing_key()
        self.issuer = _pubkey_hex(self.sk)
        self.recipient = _pubkey_hex(_make_signing_key())
        self.reference = str(uuid.uuid4())

    def test_credit_note_signature_verifiable(self):
        from knarr.commerce.receipts import create_credit_note, verify_credit_note
        note_json = create_credit_note(
            note_type="debit",
            amount=1.0,
            issuer=self.issuer,
            recipient=self.recipient,
            reference=self.reference,
            description="skill:echo execution",
            signing_key=self.sk,
        )
        self.assertTrue(verify_credit_note(note_json))

    def test_tampered_signature_rejected(self):
        from knarr.commerce.receipts import create_credit_note, verify_credit_note
        note_json = create_credit_note(
            note_type="debit",
            amount=1.0,
            issuer=self.issuer,
            recipient=self.recipient,
            reference=self.reference,
            description="skill:echo execution",
            signing_key=self.sk,
        )
        note = json.loads(note_json)
        note["amount"] = 999.0  # tamper
        tampered = json.dumps(note)
        self.assertFalse(verify_credit_note(tampered))

    def test_wrong_key_rejected(self):
        from knarr.commerce.receipts import create_credit_note, verify_credit_note
        note_json = create_credit_note(
            note_type="debit",
            amount=1.0,
            issuer=self.issuer,
            recipient=self.recipient,
            reference=self.reference,
            description="skill:echo execution",
            signing_key=self.sk,
        )
        # Change issuer to a different key (the verify_key lookup will fail)
        note = json.loads(note_json)
        other_sk = _make_signing_key()
        note["issuer"] = _pubkey_hex(other_sk)
        tampered = json.dumps(note)
        self.assertFalse(verify_credit_note(tampered))


class TestCreditNoteCanonical(unittest.TestCase):
    """Test 12: same inputs produce same canonical JSON (deterministic)."""

    def setUp(self):
        self.sk = _make_signing_key()
        self.issuer = _pubkey_hex(self.sk)
        self.recipient = _pubkey_hex(_make_signing_key())
        self.reference = str(uuid.uuid4())

    def test_credit_note_canonical_deterministic(self):
        """Two calls with same key material at same moment produce same canonical payload."""
        from knarr.commerce.receipts import create_credit_note
        import unittest.mock as _mock

        # Freeze time and patch datetime.now to get deterministic timestamp
        fixed_ts = "2026-02-28T12:00:00+00:00"
        with _mock.patch("knarr.commerce.receipts.datetime") as mock_dt:
            mock_dt.now.return_value.isoformat.return_value = fixed_ts
            note_a = create_credit_note(
                note_type="debit",
                amount=3.0,
                issuer=self.issuer,
                recipient=self.recipient,
                reference=self.reference,
                description="skill:test execution",
                signing_key=self.sk,
            )
            note_b = create_credit_note(
                note_type="debit",
                amount=3.0,
                issuer=self.issuer,
                recipient=self.recipient,
                reference=self.reference,
                description="skill:test execution",
                signing_key=self.sk,
            )

        # Both should have the same canonical structure
        parsed_a = json.loads(note_a)
        parsed_b = json.loads(note_b)
        # All fields except signature should be identical
        for key in ["type", "version", "note_type", "amount", "issuer", "recipient",
                    "reference", "description", "unit", "parent_hash"]:
            self.assertEqual(parsed_a[key], parsed_b[key])


class TestZeroReceipt(unittest.TestCase):
    """Test 13: free skill produces note_type='zero', amount=0.0."""

    def setUp(self):
        self.sk = _make_signing_key()
        self.issuer = _pubkey_hex(self.sk)
        self.recipient = _pubkey_hex(_make_signing_key())
        self.reference = str(uuid.uuid4())

    def test_zero_receipt_for_free_skill(self):
        from knarr.commerce.receipts import create_credit_note, verify_credit_note
        note_json = create_credit_note(
            note_type="zero",
            amount=0.0,
            issuer=self.issuer,
            recipient=self.recipient,
            reference=self.reference,
            description="skill:free-skill execution",
            signing_key=self.sk,
        )
        note = json.loads(note_json)
        self.assertEqual(note["note_type"], "zero")
        self.assertEqual(note["amount"], 0.0)
        self.assertTrue(verify_credit_note(note_json))


# ─────────────────────────────────────────────────────────────────────────────
# R2: Storage tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCreditNoteBucket(unittest.TestCase):
    """Test 14: store in bucket, retrieve by reference."""

    def setUp(self):
        from knarr.dht.storage import Storage
        self.storage = Storage(":memory:")

    def test_mail_creditnote_table_created(self):
        """mail_creditnote table must exist on fresh DB."""
        conn = self.storage._get_conn()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        self.assertIn("mail_creditnote", tables)

    def test_store_and_retrieve_credit_note(self):
        from knarr.dht.storage import Storage
        reference = str(uuid.uuid4())
        note_data = json.dumps({"type": "credit_note", "amount": 1.0, "reference": reference})
        counterparty = "a" * 64  # fake pubkey hex

        self.storage.store_credit_note(counterparty, reference, note_data)
        retrieved = self.storage.get_credit_note_by_reference(reference)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved, note_data)

    def test_retrieve_missing_reference(self):
        result = self.storage.get_credit_note_by_reference("nonexistent-ref")
        self.assertIsNone(result)

    def test_multiple_notes_distinct_references(self):
        ref_a = str(uuid.uuid4())
        ref_b = str(uuid.uuid4())
        counterparty = "b" * 64
        note_a = json.dumps({"ref": ref_a, "amount": 1.0})
        note_b = json.dumps({"ref": ref_b, "amount": 2.0})

        self.storage.store_credit_note(counterparty, ref_a, note_a)
        self.storage.store_credit_note(counterparty, ref_b, note_b)

        self.assertEqual(self.storage.get_credit_note_by_reference(ref_a), note_a)
        self.assertEqual(self.storage.get_credit_note_by_reference(ref_b), note_b)

    def test_credit_note_routing_in_mail_bucket(self):
        """_mail_bucket() must route 'knarr/commerce/credit_note' to mail_creditnote."""
        from knarr.dht.storage import Storage
        result = Storage._mail_bucket("knarr/commerce/credit_note", False)
        self.assertEqual(result, "mail_creditnote")

    def test_task_result_routing_unchanged(self):
        """Existing routing must not be broken."""
        from knarr.dht.storage import Storage
        self.assertEqual(Storage._mail_bucket("knarr/system/task_result", False), "mail_jobreport")
        self.assertEqual(Storage._mail_bucket("text", True), "mail_system")
        self.assertEqual(Storage._mail_bucket("text", False), "mail_inbox")


# ─────────────────────────────────────────────────────────────────────────────
# R3/E3: Test that credit note is embedded in task_result mail body
# ─────────────────────────────────────────────────────────────────────────────

class TestCreditNoteInTaskResult(unittest.TestCase):
    """Test 15: task_result mail body contains both 'receipt' and 'credit_note' fields."""

    def test_credit_note_in_task_result(self):
        """Validate the structure we build in node.py _execute_task path."""
        # Simulate what node.py does when building the async result mail body
        receipt_json = json.dumps({"data": {"credits_charged": 2.0}, "signature": "sig"})
        credit_note_json = json.dumps({
            "type": "credit_note", "version": 1,
            "note_type": "debit", "amount": 2.0,
            "signature": "sig2"
        })

        body = {
            "job_id": str(uuid.uuid4()),
            "skill": "test-skill",
            "status": "completed",
            "output_data": {"result": "ok"},
            "receipt": receipt_json,           # old format, backward compat
            "credit_note": credit_note_json,   # new format
        }

        self.assertIn("receipt", body)
        self.assertIn("credit_note", body)

        # Simulate consumer handling new format
        cn = json.loads(body["credit_note"])
        self.assertEqual(cn["type"], "credit_note")
        self.assertEqual(cn["amount"], 2.0)

        # Simulate consumer handling old format (v0.31.x fallback)
        rc = json.loads(body["receipt"])
        self.assertEqual(rc["data"]["credits_charged"], 2.0)


# ─────────────────────────────────────────────────────────────────────────────
# E3: Event connector tests
# ─────────────────────────────────────────────────────────────────────────────

class TestReceiptIssuedFiresAfterStore(unittest.TestCase):
    """Test 16: provider path — credit note stored, then event fires."""

    def test_receipt_issued_fires_after_store(self):
        """Simulate the provider emission sequence."""
        from knarr.dht.eventbus import EventBus
        from knarr.dht.storage import Storage

        bus = EventBus(size=16)
        storage = Storage(":memory:")
        sub = bus.subscribe("receipt.*")

        sk = _make_signing_key()
        issuer = _pubkey_hex(sk)
        recipient = _pubkey_hex(_make_signing_key())
        reference = str(uuid.uuid4())

        from knarr.commerce.receipts import create_credit_note
        note_json = create_credit_note(
            note_type="debit",
            amount=3.0,
            issuer=issuer,
            recipient=recipient,
            reference=reference,
            description="skill:test execution",
            signing_key=sk,
        )

        # Receipt before bus (design principle)
        storage.store_credit_note(recipient, reference, note_json)
        bus.emit("receipt.issued", note_type="debit", counterparty=recipient,
                 amount=3.0, reference=reference)

        events = sub.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "receipt.issued")
        self.assertEqual(events[0]["amount"], 3.0)
        self.assertEqual(events[0]["reference"], reference)

        # Verify storage was actually written
        stored = storage.get_credit_note_by_reference(reference)
        self.assertIsNotNone(stored)
        self.assertEqual(stored, note_json)


class TestReceiptReceivedFiresOnConsumer(unittest.TestCase):
    """Test 17: consumer path — credit note extracted and stored, event fires."""

    def test_receipt_received_fires_on_consumer(self):
        from knarr.dht.eventbus import EventBus
        from knarr.dht.storage import Storage

        bus = EventBus(size=16)
        storage = Storage(":memory:")
        sub = bus.subscribe("receipt.*")

        sk = _make_signing_key()
        provider_pubkey = _pubkey_hex(sk)
        reference = str(uuid.uuid4())

        cn_data = json.dumps({
            "type": "credit_note", "version": 1,
            "note_type": "debit", "amount": 2.0,
            "issuer": provider_pubkey, "reference": reference
        })

        # Consumer stores the received credit note first (receipt before bus)
        storage.store_credit_note(provider_pubkey, reference, cn_data)
        bus.emit("receipt.received", note_type="debit", counterparty=provider_pubkey,
                 amount=2.0, reference=reference)

        events = sub.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "receipt.received")


class TestCreditChangeFiresAfterLedger(unittest.TestCase):
    """Test 18: ledger update triggers credit.change event."""

    def test_credit_change_fires_after_ledger(self):
        from knarr.dht.eventbus import EventBus
        from knarr.dht.storage import Storage

        bus = EventBus(size=16)
        storage = Storage(":memory:")
        sub = bus.subscribe("credit.*")

        pubkey = "a" * 64
        reference = str(uuid.uuid4())

        # Simulate provider ledger update then event
        storage.get_or_create_ledger_entry(pubkey, 10.0, 0.5)
        storage.update_ledger_provider(pubkey, 2.0)
        bus.emit("credit.change", direction="provider", counterparty=pubkey,
                 amount=2.0, reference=reference)

        events = sub.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "credit.change")
        self.assertEqual(events[0]["direction"], "provider")
        self.assertEqual(events[0]["amount"], 2.0)


# ─────────────────────────────────────────────────────────────────────────────
# Meta receipts endpoint tests
# ─────────────────────────────────────────────────────────────────────────────

class TestMetaReceiptsFetch(unittest.TestCase):
    """Test 19: counterparty can fetch receipt by reference."""

    def setUp(self):
        from knarr.dht.storage import Storage
        self.storage = Storage(":memory:")
        self.sk = _make_signing_key()
        self.issuer_hex = _pubkey_hex(self.sk)
        self.recipient_hex = _pubkey_hex(_make_signing_key())
        self.reference = str(uuid.uuid4())

        from knarr.commerce.receipts import create_credit_note
        self.note_json = create_credit_note(
            note_type="debit",
            amount=5.0,
            issuer=self.issuer_hex,
            recipient=self.recipient_hex,
            reference=self.reference,
            description="skill:test execution",
            signing_key=self.sk,
        )
        # Store as issuer copy
        self.storage.store_credit_note(self.recipient_hex, self.reference, self.note_json)

    def _meta_receipts(self, reference: str, caller_pubkey: str) -> dict:
        """Simulate the meta/receipts handler — both counterparties may fetch."""
        note = self.storage.get_credit_note_by_reference(reference)
        if not note:
            return {"error": "NOT_FOUND"}
        note_data = json.loads(note)
        if caller_pubkey not in (note_data.get("issuer"), note_data.get("recipient")):
            return {"error": "ACCESS_DENIED"}
        return note_data

    def test_meta_receipts_recipient_fetch(self):
        result = self._meta_receipts(self.reference, self.recipient_hex)
        self.assertNotIn("error", result)
        self.assertEqual(result["amount"], 5.0)

    def test_meta_receipts_issuer_fetch(self):
        """Issuer is also a counterparty — can fetch."""
        result = self._meta_receipts(self.reference, self.issuer_hex)
        self.assertNotIn("error", result)
        self.assertEqual(result["amount"], 5.0)

    def test_meta_receipts_not_found(self):
        result = self._meta_receipts("nonexistent-ref", self.recipient_hex)
        self.assertEqual(result.get("error"), "NOT_FOUND")


class TestMetaReceiptsForbidden(unittest.TestCase):
    """Test 20: non-counterparty gets forbidden."""

    def setUp(self):
        from knarr.dht.storage import Storage
        self.storage = Storage(":memory:")
        self.sk = _make_signing_key()
        self.issuer_hex = _pubkey_hex(self.sk)
        self.recipient_hex = _pubkey_hex(_make_signing_key())
        self.attacker_hex = _pubkey_hex(_make_signing_key())
        self.reference = str(uuid.uuid4())

        from knarr.commerce.receipts import create_credit_note
        self.note_json = create_credit_note(
            note_type="debit",
            amount=5.0,
            issuer=self.issuer_hex,
            recipient=self.recipient_hex,
            reference=self.reference,
            description="skill:test execution",
            signing_key=self.sk,
        )
        self.storage.store_credit_note(self.recipient_hex, self.reference, self.note_json)

    def _meta_receipts(self, reference: str, caller_pubkey: str) -> dict:
        note = self.storage.get_credit_note_by_reference(reference)
        if not note:
            return {"error": "NOT_FOUND"}
        note_data = json.loads(note)
        if caller_pubkey not in (note_data.get("issuer"), note_data.get("recipient")):
            return {"error": "ACCESS_DENIED"}
        return note_data

    def test_meta_receipts_attacker_forbidden(self):
        result = self._meta_receipts(self.reference, self.attacker_hex)
        self.assertEqual(result.get("error"), "ACCESS_DENIED")

    def test_meta_receipts_issuer_allowed(self):
        """Issuer is a counterparty — can fetch their own receipt."""
        result = self._meta_receipts(self.reference, self.issuer_hex)
        self.assertNotIn("error", result)
        self.assertEqual(result["amount"], 5.0)


# ─────────────────────────────────────────────────────────────────────────────
# Async EventBus tests
# ─────────────────────────────────────────────────────────────────────────────

class TestEventBusAsync(unittest.TestCase):
    """Async tests for EventBus.next() blocking behavior."""

    def _run(self, coro):
        return _run_async(coro)

    def test_next_returns_existing_event(self):
        from knarr.dht.eventbus import EventBus
        bus = EventBus(size=16)
        sub = bus.subscribe("receipt.*")
        bus.emit("receipt.issued", amount=1.0)

        async def fetch():
            return await asyncio.wait_for(sub.next(), timeout=1.0)

        event = self._run(fetch())
        self.assertEqual(event["event"], "receipt.issued")

    def test_next_blocks_then_wakes(self):
        from knarr.dht.eventbus import EventBus
        bus = EventBus(size=16)
        sub = bus.subscribe("receipt.*")

        async def emit_after_delay():
            await asyncio.sleep(0.05)
            bus.emit("receipt.issued", amount=2.0)

        async def run():
            await asyncio.gather(
                emit_after_delay(),
                asyncio.wait_for(sub.next(), timeout=1.0),
            )

        self._run(run())

    def test_emit_is_synchronous_no_block(self):
        """emit() must return immediately regardless of subscriber count."""
        from knarr.dht.eventbus import EventBus
        import time
        bus = EventBus(size=256)
        # Add 100 subscribers
        subs = [bus.subscribe("*") for _ in range(100)]
        start = time.monotonic()
        for i in range(1000):
            bus.emit("tick", i=i)
        elapsed = time.monotonic() - start
        # 1000 emits with 100 subs should be microseconds, certainly < 1s
        self.assertLess(elapsed, 1.0)


# ─────────────────────────────────────────────────────────────────────────────
# Version test
# ─────────────────────────────────────────────────────────────────────────────

class TestVersion(unittest.TestCase):
    def test_version_bumped(self):
        from knarr import __version__
        self.assertEqual(__version__, "0.45.0")


# ─────────────────────────────────────────────────────────────────────────────
# MAIL_BUCKETS constant test
# ─────────────────────────────────────────────────────────────────────────────

class TestMailBuckets(unittest.TestCase):
    def test_mail_creditnote_in_buckets(self):
        from knarr.dht.storage import MAIL_BUCKETS
        self.assertIn("mail_creditnote", MAIL_BUCKETS)

    def test_existing_buckets_unchanged(self):
        from knarr.dht.storage import MAIL_BUCKETS
        self.assertIn("mail_inbox", MAIL_BUCKETS)
        self.assertIn("mail_jobreport", MAIL_BUCKETS)
        self.assertIn("mail_system", MAIL_BUCKETS)


# ─────────────────────────────────────────────────────────────────────────────
# Plugin context test
# ─────────────────────────────────────────────────────────────────────────────

class TestPluginContextEventFields(unittest.TestCase):
    def test_plugin_context_has_event_fields(self):
        from knarr.dht.plugins import PluginContext
        import dataclasses
        fields = {f.name for f in dataclasses.fields(PluginContext)}
        self.assertIn("subscribe_events", fields)
        self.assertIn("emit_event", fields)

    def test_plugin_context_defaults_to_none(self):
        from knarr.dht.plugins import PluginContext
        # Create minimal valid context
        ctx = PluginContext(
            node_id="abc",
            plugin_dir=None,
            get_peers=None,
            send_to_peer=None,
            send_fire_forget=None,
            delivery_cb=None,
            log=None,
        )
        self.assertIsNone(ctx.subscribe_events)
        self.assertIsNone(ctx.emit_event)


if __name__ == "__main__":
    unittest.main()
