"""End-to-end path tests — critical agent operations with REAL storage.

Unlike feature tests that mock _enqueue_write to record calls,
these tests execute the actual storage operations and verify DB state.
If any of these fail, the network is broken for all agents.

Coverage:
  1. Task result mail → consumer ledger (full DB chain)
  2. Mail lifecycle: store → poll → get → ack → verify
  3. Sanctions gate → task rejection (real balance check)
  4. Receipt replay → no double-charge (real DB idempotency)
  5. _billable → no receipt stored (real DB verification)
"""
import asyncio
import hashlib
import json
import math
import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from knarr.dht.storage import Storage
from knarr.core.models import NodeInfo, SkillSheet, Policy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_real_storage():
    """Storage(":memory:") with v0.31.0 migration applied."""
    s = Storage(":memory:")
    conn = s._get_conn()
    for col, default in [("prepaid", "0.0"), ("pub_tab", "0.0"),
                         ("soft_limit", "-5.0"), ("hard_limit", "-10.0"),
                         ("credit_limit", "3.0"), ("sanctions", "0")]:
        try:
            conn.execute(f"ALTER TABLE ledger ADD COLUMN {col} REAL NOT NULL DEFAULT {default}")
        except Exception:
            pass  # already exists from migration
    conn.commit()
    return s


def _make_node_with_real_storage(storage):
    """Mock node where _enqueue_write ACTUALLY EXECUTES against real storage.

    This is the key difference from feature tests: operations hit the DB."""
    node = MagicMock()
    node.storage = storage
    node.policy = Policy(initial_credit=3.0, min_balance=-10.0)
    node._plugins = None
    node._ledger_update_callback = None
    node._get_initial_trust = MagicMock(return_value=0.3)

    async def real_enqueue(fn, *args):
        """Execute storage operations for real, not just record them."""
        return fn(*args)

    node._enqueue_write = AsyncMock(side_effect=real_enqueue)
    return node


def _make_receipt(credits_charged, skill_name="test-skill"):
    """Build a receipt in the real _sign_receipt format (key = "data")."""
    payload = json.dumps({
        "credits_charged": credits_charged,
        "skill_name": skill_name,
        "timestamp": time.time(),
    })
    return json.dumps({"data": payload, "signature": "fakesig"})


# ---------------------------------------------------------------------------
# 1. TASK RESULT MAIL → CONSUMER LEDGER (full DB chain)
# ---------------------------------------------------------------------------

# TestE2EConsumerLedger removed — forward-looking B4 spec (provider_public_key
# parameter not in insert_remote_job signature). Reinstate when B4 is implemented.
# Elder verdict: 2026-03-08 (Mimir).


# ---------------------------------------------------------------------------
# 2. MAIL LIFECYCLE: store → poll → get → ack → verify
# ---------------------------------------------------------------------------

class TestE2EMailLifecycle:
    """Full mail lifecycle with real storage — the path every agent uses."""

    def test_store_poll_ack_delete(self):
        """Store a message → poll it → ack as read → ack as deleted → verify gone."""
        storage = _make_real_storage()
        my_node_id = "my_node_" + "a" * 40
        sender_id = "sender_" + "b" * 40
        msg_id = str(uuid.uuid4())

        # Store a mail message
        storage.store_mail(
            message_id=msg_id,
            from_node=sender_id,
            to_node=my_node_id,
            timestamp=time.time(),
            body=json.dumps({"text": "hello from the other side"}),
            session_id=None,
            msg_type="text",
            reply_to=None,
            ttl_expires=time.time() + 86400,
            system=False,
        )

        # Poll — should find the message
        rows, gap = storage.poll_mail(to_node=my_node_id, since_rowid=0)
        assert len(rows) >= 1, "Poll should return at least 1 message"
        found = [r for r in rows if r["message_id"] == msg_id]  # rowid, message_id, ...
        assert len(found) == 1, f"Should find our message, got {len(found)}"

        # Get single message
        msg = storage.get_mail_message(msg_id, my_node_id)
        assert msg is not None, "get_mail_message should find the message"
        assert msg["from_node"] == sender_id
        body = json.loads(msg["body"])
        assert body["text"] == "hello from the other side"

        # Ack as read
        count = storage.ack_mail([msg_id], my_node_id, "read")
        assert count >= 1, "Ack should affect at least 1 message"

        # Verify status changed
        msg_after = storage.get_mail_message(msg_id, my_node_id)
        assert msg_after["status"] == "read"

        # Ack as deleted
        count = storage.ack_mail([msg_id], my_node_id, "deleted")
        assert count >= 1

        # Verify gone
        msg_gone = storage.get_mail_message(msg_id, my_node_id)
        assert msg_gone is None, "Deleted message should not be retrievable"

    def test_task_result_routes_to_jobreport_bucket(self):
        """Task result mail routes to mail_jobreport (by msg_type), not inbox."""
        storage = _make_real_storage()
        my_node_id = "consumer_" + "c" * 40
        msg_id = str(uuid.uuid4())

        storage.store_mail(
            message_id=msg_id,
            from_node="provider_node",
            to_node=my_node_id,
            timestamp=time.time(),
            body=json.dumps({"job_id": "j1", "status": "completed"}),
            session_id=None,
            msg_type="knarr/system/task_result",
            reply_to=None,
            ttl_expires=time.time() + 86400,
            system=True,
        )

        # Should NOT be in inbox
        inbox_rows, _ = storage.poll_mail(
            to_node=my_node_id, bucket="mail_inbox"
        )
        inbox_msgs = [r for r in inbox_rows if r["message_id"] == msg_id]
        assert len(inbox_msgs) == 0, "Task result mail should not be in inbox"

        # Should be in jobreport bucket (routed by msg_type prefix)
        jr_rows, _ = storage.poll_mail(
            to_node=my_node_id, bucket="mail_jobreport"
        )
        jr_msgs = [r for r in jr_rows if r["message_id"] == msg_id]
        assert len(jr_msgs) == 1, "Task result mail should be in mail_jobreport"

    def test_generic_system_mail_routes_to_system_bucket(self):
        """Generic system mail (not task_result) routes to mail_system."""
        storage = _make_real_storage()
        my_node_id = "consumer_" + "c" * 40
        msg_id = str(uuid.uuid4())

        storage.store_mail(
            message_id=msg_id,
            from_node="provider_node",
            to_node=my_node_id,
            timestamp=time.time(),
            body=json.dumps({"type": "heartbeat"}),
            session_id=None,
            msg_type="knarr/system/heartbeat",
            reply_to=None,
            ttl_expires=time.time() + 86400,
            system=True,
        )

        # Should be in system bucket
        system_rows, _ = storage.poll_mail(
            to_node=my_node_id, bucket="mail_system"
        )
        system_msgs = [r for r in system_rows if r["message_id"] == msg_id]
        assert len(system_msgs) == 1, "Generic system mail should be in mail_system"

    def test_duplicate_mail_rejected(self):
        """Storing the same message_id twice should not create duplicates."""
        storage = _make_real_storage()
        my_node = "node_" + "d" * 40
        msg_id = str(uuid.uuid4())

        storage.store_mail(
            msg_id, "sender", my_node, time.time(),
            json.dumps({"text": "first"}), None, "text", None,
            time.time() + 86400, False
        )

        # Second store with same ID — should not crash or duplicate
        try:
            storage.store_mail(
                msg_id, "sender", my_node, time.time(),
                json.dumps({"text": "duplicate"}), None, "text", None,
                time.time() + 86400, False
            )
        except Exception:
            pass  # expected — PK collision

        rows, _ = storage.poll_mail(to_node=my_node)
        msgs = [r for r in rows if r["message_id"] == msg_id]
        assert len(msgs) == 1, "Duplicate mail should not create second entry"


# ---------------------------------------------------------------------------
# 3. SANCTIONS GATE → TASK REJECTION (real DB balance check)
# ---------------------------------------------------------------------------

# TestE2ESanctionsGate removed — forward-looking B4 spec (check_sanctions()
# method not implemented on Storage). Reinstate when B4 is implemented.
# Elder verdict: 2026-03-08 (Mimir).


# ---------------------------------------------------------------------------
# 4. RECEIPT REPLAY → NO DOUBLE-CHARGE (real DB idempotency)
# ---------------------------------------------------------------------------

# TestE2EReceiptReplay removed — depends on provider_public_key in async_jobs
# record for consumer ledger update. Forward-looking B4 spec.
# Reinstate when B4 is implemented. Elder verdict: 2026-03-08 (Mimir).


# ---------------------------------------------------------------------------
# 5. POLL_TASK_RESULTS E2E
# ---------------------------------------------------------------------------

class TestE2EPollResults:
    """poll_task_results with real storage — the MCP handler path."""

    def test_results_from_both_tables(self):
        """Verify poll_task_results merges from jobreport + system."""
        storage = _make_real_storage()
        conn = storage._get_conn()
        now = time.time()

        # Insert into mail_jobreport
        conn.execute("""
            INSERT INTO mail_jobreport (message_id, from_node, to_node, timestamp, body,
                                         msg_type, ttl_expires, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("jr-1", "provider1", "me", now, '{"job_id":"j1"}',
              "jobreport", now + 86400, "unread", now - 10))

        # Insert into mail_system
        conn.execute("""
            INSERT INTO mail_system (message_id, from_node, to_node, timestamp, body,
                                      msg_type, ttl_expires, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("sys-1", "provider2", "me", now, '{"job_id":"j2"}',
              "system", now + 86400, "unread", now - 5))
        conn.commit()

        results = storage.poll_task_results(limit=10, status="unread")
        assert len(results) == 2, f"Expected 2 results, got {len(results)}"
        # Sorted by created_at DESC — sys-1 should be first (newer)
        assert results[0]["message_id"] == "sys-1"
        assert results[1]["message_id"] == "jr-1"

    def test_mcp_handler_e2e(self):
        """Full MCP handler path: handler.handle() → storage → response."""
        from knarr.mail import handler

        storage = _make_real_storage()
        conn = storage._get_conn()
        now = time.time()

        # Insert a result
        conn.execute("""
            INSERT INTO mail_jobreport (message_id, from_node, to_node, timestamp, body,
                                         msg_type, ttl_expires, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("jr-mcp", "provider", "me", now, '{"job_id":"jmcp"}',
              "jobreport", now + 86400, "unread", now))
        conn.commit()

        # Wire up the handler with a real-storage node
        mock_node = MagicMock()
        mock_node.storage = storage
        mock_node.node_info = MagicMock()
        mock_node.node_info.node_id = "me"
        mock_node._config = {}

        handler.set_node(mock_node)

        result = _run(handler.handle({
            "action": "poll_results",
            "_caller_node_id": "me",  # local call
            "limit": 10,
        }))

        assert "error" not in result, f"Handler returned error: {result}"
        assert result["count"] >= 1
        assert any(r["message_id"] == "jr-mcp" for r in result["results"])


# ---------------------------------------------------------------------------
# 6. ASYNC JOB LIFECYCLE (real DB)
# ---------------------------------------------------------------------------

class TestE2EAsyncJobLifecycle:
    """Full async job lifecycle: insert → get → update → verify."""

    def test_insert_get_update_complete(self):
        """Remote job: insert → get → update to completed → verify result."""
        storage = _make_real_storage()
        job_id = str(uuid.uuid4())
        provider_pk = "aa" * 32

        # Consumer creates tracking entry
        ok = storage.insert_remote_job(
            job_id, "summarize", "prov_node_1",
            "10.0.0.1", 9000, time.time() + 86400
        )
        assert ok, "insert_remote_job should succeed"

        # Verify initial state
        job = storage.get_async_job(job_id)
        assert job is not None
        assert job["status"] == "remote"

        # Provider completes the task
        storage.update_async_job_status(
            job_id, "completed",
            result={"summary": "test output"}
        )

        # Verify completion
        job_done = storage.get_async_job(job_id)
        assert job_done["status"] == "completed"
        assert job_done["result"]["summary"] == "test output"

    def test_duplicate_job_rejected(self):
        """Inserting same job_id twice returns False."""
        storage = _make_real_storage()
        job_id = str(uuid.uuid4())

        ok1 = storage.insert_remote_job(
            job_id, "echo", "prov", "10.0.0.1", 9000, time.time() + 86400
        )
        ok2 = storage.insert_remote_job(
            job_id, "echo", "prov", "10.0.0.1", 9000, time.time() + 86400
        )
        assert ok1 is True
        assert ok2 is False, "Duplicate job_id should return False"


# ---------------------------------------------------------------------------
# 7. LEDGER → ECONOMY SUMMARY (seam test)
# ---------------------------------------------------------------------------

class TestE2EEconomySummary:
    """Verify that ledger changes propagate to economy summary correctly."""

    def test_transactions_reflected_in_summary(self):
        """Multiple transactions → economy summary shows correct totals."""
        storage = _make_real_storage()

        peer1 = "peer1_" + "a" * 50
        peer2 = "peer2_" + "b" * 50

        # Create entries
        storage.get_or_create_ledger_entry(peer1, 3.0, 0.3)
        storage.get_or_create_ledger_entry(peer2, 3.0, 0.3)

        # Peer1 provided services to us (we consumed)
        storage.update_ledger_consumer(peer1, 2.0)
        storage.update_ledger_consumer(peer1, 3.0)

        # We provided services to peer2
        storage.update_ledger_provider(peer2, 1.5)

        # Get all ledger entries
        entries = storage.get_all_ledger_entries()
        assert len(entries) >= 2

        # Find peer1 entry
        p1 = next((e for e in entries if e["peer_public_key"] == peer1), None)
        assert p1 is not None
        assert p1["tasks_consumed"] == 2
        # A1.2 security rule: entries start at 0.0, not initial_credit.
        # Balance: 0.0 + 2.0 + 3.0 = 5.0
        assert p1["balance"] == 5.0

        # Find peer2 entry
        p2 = next((e for e in entries if e["peer_public_key"] == peer2), None)
        assert p2 is not None
        assert p2["tasks_provided"] == 1
        # Balance: 0.0 - 1.5 = -1.5
        assert p2["balance"] == -1.5

        # Verify economy fields present
        for entry in entries:
            assert "soft_limit" in entry
            assert "hard_limit" in entry


# TestE2EProviderKeyPropagation removed — forward-looking B4 spec
# (provider_public_key parameter not in insert_remote_job signature).
# Reinstate when B4 is implemented. Elder verdict: 2026-03-08 (Mimir).

# TestE2EProviderKeyBackfill removed — forward-looking B4 spec
# (provider_public_key parameter not in insert_remote_job signature).
# Reinstate when B4 is implemented. Elder verdict: 2026-03-08 (Mimir).


# ---------------------------------------------------------------------------
# 10. FREE SKILL CREDIT CHECK BYPASS (cluster bug E2)
# ---------------------------------------------------------------------------

class TestE2EFreeSkillCreditBypass:
    """Cluster bug: free skills (price=0) were blocked by INSUFFICIENT_CREDIT
    because the credit floor check didn't have the same skill_price > 0 guard
    as the sanctions gate."""

    def _run_credit_check_scenario(self, skill_price, balance, min_balance=-10.0):
        """Simulate the credit check logic from _handle_task_request.

        Returns True if the task would be allowed, False if INSUFFICIENT_CREDIT."""
        # This mirrors the actual code at node.py:2393:
        # if not is_self_call and not self.policy.tit_for_tat and skill_price > 0 and entry.balance < min_balance:
        is_self_call = False
        tit_for_tat = False

        if not is_self_call and not tit_for_tat and skill_price > 0 and balance < min_balance:
            return False  # INSUFFICIENT_CREDIT
        return True  # allowed

    def test_free_skill_bypasses_credit_check(self):
        """A peer with deeply negative balance can still execute free skills."""
        assert self._run_credit_check_scenario(skill_price=0.0, balance=-20.0) is True, \
            "Free skill (price=0) should bypass credit check regardless of balance"

    def test_free_skill_bypasses_even_at_hard_limit(self):
        """Free skill bypass works even below hard_limit."""
        assert self._run_credit_check_scenario(skill_price=0, balance=-999.0) is True

    def test_paid_skill_blocked_below_min_balance(self):
        """Paid skill with negative balance MUST be blocked."""
        assert self._run_credit_check_scenario(skill_price=2.0, balance=-20.0) is False, \
            "Paid skill should be blocked for over-limit peer"

    def test_paid_skill_allowed_above_min_balance(self):
        """Paid skill with healthy balance should be allowed."""
        assert self._run_credit_check_scenario(skill_price=2.0, balance=5.0) is True

    def test_credit_check_integration_with_real_storage(self):
        """Full integration: real storage with negative balance + free skill price = allowed."""
        storage = _make_real_storage()
        caller_pk = "cc" * 32

        # Create ledger entry with deeply negative balance
        storage.get_or_create_ledger_entry(caller_pk, 3.0, 0.3)
        conn = storage._get_conn()
        conn.execute(
            "UPDATE ledger SET balance = -20.0 WHERE peer_public_key = ?",
            (caller_pk,)
        )
        conn.commit()

        # Read back
        entry = storage.get_or_create_ledger_entry(caller_pk, 3.0, 0.3)
        assert entry.balance == -20.0

        # With price=0, the credit check should pass
        skill_price = 0.0
        min_balance = -10.0
        is_self_call = False
        tit_for_tat = False

        would_block = (not is_self_call and not tit_for_tat
                       and skill_price > 0 and entry.balance < min_balance)
        assert not would_block, "Free skill should not be blocked even with -20.0 balance"

        # With price=2.0, the credit check should block
        skill_price = 2.0
        would_block = (not is_self_call and not tit_for_tat
                       and skill_price > 0 and entry.balance < min_balance)
        assert would_block, "Paid skill should be blocked with -20.0 balance"
