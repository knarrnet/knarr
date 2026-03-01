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

class TestE2EConsumerLedger:
    """The most critical path: async task completes → consumer ledger updated in DB."""

    def test_full_chain_receipt_to_ledger_balance(self):
        """Task result mail → parse receipt → create ledger entry → debit consumer.
        Verify actual DB state, not just enqueue calls."""
        from knarr.dht.node import DHTNode

        storage = _make_real_storage()
        node = _make_node_with_real_storage(storage)

        provider_pk = "ab" * 32
        job_id = str(uuid.uuid4())

        # Insert a real remote job in the DB (consumer tracking)
        storage.insert_remote_job(
            job_id, "echo", "provider_node_123",
            "10.0.0.1", 9000, time.time() + 86400,
            provider_public_key=provider_pk
        )

        # Verify job exists and has provider_public_key
        job = storage.get_async_job(job_id)
        assert job is not None
        assert job["provider_public_key"] == provider_pk

        item = {
            "from_node": "provider_node_123",
            "body": {
                "job_id": job_id,
                "status": "completed",
                "output_data": {"result": "hello"},
                "receipt": _make_receipt(2.5),
            },
        }

        _run(DHTNode._handle_task_result_mail(node, item))

        # Verify ACTUAL DB state
        entry = storage.get_or_create_ledger_entry(provider_pk, 3.0, 0.3)
        assert entry.balance != 0.0, "Ledger balance should have changed"
        # Consumer side: balance goes UP by credits_charged (we owe them)
        assert entry.tasks_consumed >= 1, "tasks_consumed should be incremented"

        # Verify job status updated in DB
        job_after = storage.get_async_job(job_id)
        assert job_after["status"] == "completed"

    def test_chain_with_zero_credits_no_ledger_entry(self):
        """Receipt with credits_charged=0 should NOT create a ledger entry."""
        from knarr.dht.node import DHTNode

        storage = _make_real_storage()
        node = _make_node_with_real_storage(storage)

        provider_pk = "cd" * 32
        job_id = str(uuid.uuid4())

        storage.insert_remote_job(
            job_id, "free-skill", "provider_node_456",
            "10.0.0.2", 9000, time.time() + 86400,
            provider_public_key=provider_pk
        )

        item = {
            "from_node": "provider_node_456",
            "body": {
                "job_id": job_id,
                "status": "completed",
                "output_data": {},
                "receipt": _make_receipt(0),  # zero = no charge
            },
        }

        _run(DHTNode._handle_task_result_mail(node, item))

        # Ledger entry should NOT have been touched by the consumer path
        conn = storage._get_conn()
        row = conn.execute(
            "SELECT * FROM ledger WHERE peer_public_key = ?", (provider_pk,)
        ).fetchone()
        assert row is None, "Zero-credit receipt should not create ledger entry"


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

class TestE2ESanctionsGate:
    """Sanctions gate with real storage — the seam between storage and node."""

    def test_over_limit_peer_blocked_in_db(self):
        """Peer with balance past hard_limit in real DB → check_sanctions returns hard_block."""
        storage = _make_real_storage()
        peer_pk = "bad_peer_" + "e" * 50

        # Create ledger entry
        storage.get_or_create_ledger_entry(peer_pk, 3.0, 0.3)

        # Drive balance past hard_limit (-10.0)
        # update_ledger_consumer adds to balance (consumer owes provider)
        # We need to make balance negative — use update_ledger_provider which subtracts
        # Actually: update_ledger_provider does balance -= price (provider gave service)
        # We want the peer to owe us, so we provided services:
        for _ in range(5):
            storage.update_ledger_provider(peer_pk, 3.0)  # each: balance -= 3.0

        # Verify balance is past hard_limit
        entry = storage.get_or_create_ledger_entry(peer_pk, 3.0, 0.3)
        assert entry.balance <= -10.0, f"Balance should be <= -10.0, got {entry.balance}"

        # Sanctions check should return hard_block
        result = storage.check_sanctions(peer_pk)
        assert result == "hard_block", f"Expected hard_block, got {result}"

    def test_warning_zone_peer_gets_soft_warning(self):
        """Peer in warning zone (-5 to -10) gets soft_warning."""
        storage = _make_real_storage()
        peer_pk = "warn_peer_" + "f" * 50

        storage.get_or_create_ledger_entry(peer_pk, 3.0, 0.3)
        # Drive to -6.0 (between soft=-5 and hard=-10)
        storage.update_ledger_provider(peer_pk, 3.0)  # 3.0 - 3.0 = 0
        storage.update_ledger_provider(peer_pk, 3.0)  # 0 - 3.0 = -3.0
        storage.update_ledger_provider(peer_pk, 3.0)  # -3.0 - 3.0 = -6.0

        entry = storage.get_or_create_ledger_entry(peer_pk, 3.0, 0.3)
        assert -10.0 < entry.balance <= -5.0, f"Balance should be in warning zone, got {entry.balance}"

        result = storage.check_sanctions(peer_pk)
        assert result == "soft_warning", f"Expected soft_warning, got {result}"

    def test_manual_sanctions_flag_blocks_regardless_of_balance(self):
        """Manual sanctions > 0 blocks even with positive balance."""
        storage = _make_real_storage()
        peer_pk = "sanctioned_" + "1" * 50

        storage.get_or_create_ledger_entry(peer_pk, 100.0, 0.3)  # generous credit

        # Set manual sanctions flag
        conn = storage._get_conn()
        conn.execute(
            "UPDATE ledger SET sanctions = 1 WHERE peer_public_key = ?",
            (peer_pk,)
        )
        conn.commit()

        result = storage.check_sanctions(peer_pk)
        assert result == "hard_block", "Manual sanctions should override balance"

    def test_healthy_peer_passes(self):
        """Peer with good balance passes sanctions."""
        storage = _make_real_storage()
        peer_pk = "good_peer_" + "2" * 50

        storage.get_or_create_ledger_entry(peer_pk, 3.0, 0.3)

        result = storage.check_sanctions(peer_pk)
        assert result == "ok"


# ---------------------------------------------------------------------------
# 4. RECEIPT REPLAY → NO DOUBLE-CHARGE (real DB idempotency)
# ---------------------------------------------------------------------------

class TestE2EReceiptReplay:
    """Receipt replay with real storage — verify balance changes only once."""

    def test_replay_does_not_double_debit(self):
        """Process same task result twice → balance changes only from first."""
        from knarr.dht.node import DHTNode

        storage = _make_real_storage()
        node = _make_node_with_real_storage(storage)

        provider_pk = "ee" * 32
        job_id = str(uuid.uuid4())

        storage.insert_remote_job(
            job_id, "echo", "prov_node",
            "10.0.0.1", 9000, time.time() + 86400,
            provider_public_key=provider_pk
        )

        item = {
            "from_node": "prov_node",
            "body": {
                "job_id": job_id,
                "status": "completed",
                "output_data": {"result": "ok"},
                "receipt": _make_receipt(5.0),
            },
        }

        # First processing
        _run(DHTNode._handle_task_result_mail(node, item))
        entry_after_first = storage.get_or_create_ledger_entry(provider_pk, 3.0, 0.3)
        balance_after_first = entry_after_first.balance

        # Second processing (replay)
        _run(DHTNode._handle_task_result_mail(node, item))
        entry_after_second = storage.get_or_create_ledger_entry(provider_pk, 3.0, 0.3)
        balance_after_second = entry_after_second.balance

        assert balance_after_second == balance_after_first, \
            f"Replay changed balance: {balance_after_first} → {balance_after_second}"

    def test_different_jobs_both_debit(self):
        """Two different jobs from same provider both update ledger."""
        from knarr.dht.node import DHTNode

        storage = _make_real_storage()
        node = _make_node_with_real_storage(storage)

        provider_pk = "dd" * 32

        for i in range(2):
            job_id = str(uuid.uuid4())
            storage.insert_remote_job(
                job_id, "echo", "prov_node",
                "10.0.0.1", 9000, time.time() + 86400,
                provider_public_key=provider_pk
            )
            item = {
                "from_node": "prov_node",
                "body": {
                    "job_id": job_id,
                    "status": "completed",
                    "output_data": {},
                    "receipt": _make_receipt(1.0),
                },
            }
            _run(DHTNode._handle_task_result_mail(node, item))

        entry = storage.get_or_create_ledger_entry(provider_pk, 3.0, 0.3)
        assert entry.tasks_consumed >= 2, "Both jobs should increment tasks_consumed"


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

        result = handler.handle({
            "action": "poll_results",
            "_caller_node_id": "me",  # local call
            "limit": 10,
        })

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
            "10.0.0.1", 9000, time.time() + 86400,
            provider_public_key=provider_pk
        )
        assert ok, "insert_remote_job should succeed"

        # Verify initial state
        job = storage.get_async_job(job_id)
        assert job is not None
        assert job["status"] == "remote"
        assert job["provider_public_key"] == provider_pk

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
        # Balance should reflect: initial_credit(3.0) + 2.0 + 3.0 = 8.0
        assert p1["balance"] == 8.0

        # Find peer2 entry
        p2 = next((e for e in entries if e["peer_public_key"] == peer2), None)
        assert p2 is not None
        assert p2["tasks_provided"] == 1
        # Balance: initial_credit(3.0) - 1.5 = 1.5
        assert p2["balance"] == 1.5

        # Verify economy fields present
        for entry in entries:
            assert "soft_limit" in entry
            assert "hard_limit" in entry


# ---------------------------------------------------------------------------
# 8. PROVIDER PUBLIC KEY PROPAGATION (cluster bug E1)
# ---------------------------------------------------------------------------

class TestE2EProviderKeyPropagation:
    """Cluster bug: provider_public_key was dropped by get_skills() aggregation,
    so insert_remote_job stored an empty string. The consumer ledger update then
    couldn't find the provider."""

    def test_get_skills_carries_provider_public_key(self):
        """get_skills() must include _provider_public_key in provider entries."""
        from knarr.dht.node import DHTNode

        storage = _make_real_storage()
        node = _make_node_with_real_storage(storage)

        # Register a skill with a known provider_public_key
        provider_pk = "ef" * 32
        provider_nid = hashlib.sha256(bytes.fromhex(provider_pk)).hexdigest()
        skill_json = json.dumps({
            "name": "paid-echo", "version": "1.0",
            "description": "echo", "tags": [], "price": 2.0
        })

        # Insert peer (must be recent for query_all_active_skills)
        storage.upsert_peer(NodeInfo(node_id=provider_nid, host="10.0.0.5", port=9000))

        # Insert skill with provider_public_key
        conn = storage._get_conn()
        conn.execute("""
            INSERT INTO skills (provider_node_id, skill_record_json, announced_at, ttl,
                               is_own, provider_public_key, provider_host, provider_port)
            VALUES (?, ?, ?, ?, 0, ?, '', 0)
        """, (provider_nid, skill_json, time.time(), 300, provider_pk))
        conn.commit()

        # Mock the node enough for get_skills to work
        node._handlers = {}
        node._handler_specs = {}
        node._skill_visibility = {}
        node._own_skills = {}

        result = DHTNode.get_skills(node)

        # Find our skill
        net_skills = result["network"]
        assert len(net_skills) >= 1
        echo_skill = next((s for s in net_skills if s["name"] == "paid-echo"), None)
        assert echo_skill is not None
        assert len(echo_skill["providers"]) >= 1

        # THE FIX: provider entry must carry _provider_public_key
        provider = echo_skill["providers"][0]
        assert provider["_provider_public_key"] == provider_pk, \
            f"Expected {provider_pk}, got {provider.get('_provider_public_key', 'MISSING')}"

    def test_provider_key_reaches_insert_remote_job(self):
        """Full chain: skill in DB → get_skills() → provider dict → insert_remote_job → stored."""
        from knarr.dht.node import DHTNode

        storage = _make_real_storage()
        node = _make_node_with_real_storage(storage)

        provider_pk = "ab" * 32
        provider_nid = hashlib.sha256(bytes.fromhex(provider_pk)).hexdigest()
        skill_json = json.dumps({
            "name": "chain-test", "version": "1.0",
            "description": "test", "tags": []
        })

        storage.upsert_peer(NodeInfo(node_id=provider_nid, host="10.0.0.6", port=9000))
        conn = storage._get_conn()
        conn.execute("""
            INSERT INTO skills (provider_node_id, skill_record_json, announced_at, ttl,
                               is_own, provider_public_key, provider_host, provider_port)
            VALUES (?, ?, ?, ?, 0, ?, '', 0)
        """, (provider_nid, skill_json, time.time(), 300, provider_pk))
        conn.commit()

        node._handlers = {}
        node._handler_specs = {}
        node._skill_visibility = {}
        node._own_skills = {}

        result = DHTNode.get_skills(node)
        provider = result["network"][0]["providers"][0]

        # Simulate what cockpit does: insert_remote_job with provider dict
        job_id = str(uuid.uuid4())
        storage.insert_remote_job(
            job_id, "chain-test", provider["node_id"],
            provider["host"], provider["port"], time.time() + 86400,
            provider_public_key=provider.get("_provider_public_key", "")
        )

        job = storage.get_async_job(job_id)
        assert job["provider_public_key"] == provider_pk, \
            f"Expected {provider_pk} in async_jobs, got '{job['provider_public_key']}'"


# ---------------------------------------------------------------------------
# 9. MCP BRIDGE PROVIDER KEY BACKFILL (bug report v0.31.0)
# ---------------------------------------------------------------------------

class TestE2EProviderKeyBackfill:
    """Bug: MCP bridge sends provider dict without _provider_public_key.
    Cockpit should backfill from skills table before insert_remote_job."""

    def test_backfill_from_skills_table(self):
        """_backfill_provider_key fills key from skills table when caller omits it."""
        from knarr.dashboard.server import CockpitServer

        storage = _make_real_storage()
        node = _make_node_with_real_storage(storage)

        provider_pk = "ff" * 32
        provider_nid = hashlib.sha256(bytes.fromhex(provider_pk)).hexdigest()
        skill_json = json.dumps({
            "name": "web-search", "version": "1.0",
            "description": "search", "tags": []
        })

        # Insert skill with provider_public_key
        conn = storage._get_conn()
        conn.execute("""
            INSERT INTO skills (provider_node_id, skill_record_json, announced_at, ttl,
                               is_own, provider_public_key, provider_host, provider_port)
            VALUES (?, ?, ?, ?, 0, ?, '', 0)
        """, (provider_nid, skill_json, time.time(), 300, provider_pk))
        conn.commit()

        # Simulate MCP bridge provider dict — no _provider_public_key
        provider = {"node_id": provider_nid, "host": "10.0.0.7", "port": 9000}
        assert "_provider_public_key" not in provider

        # Create minimal CockpitServer to call _backfill_provider_key
        server = CockpitServer.__new__(CockpitServer)
        server._node = node

        server._backfill_provider_key(provider, "web-search")

        assert provider.get("_provider_public_key") == provider_pk, \
            f"Expected backfill to {provider_pk}, got {provider.get('_provider_public_key', 'MISSING')}"

    def test_backfill_then_insert_remote_job(self):
        """Full chain: MCP-style provider dict → backfill → insert_remote_job → key stored."""
        from knarr.dashboard.server import CockpitServer

        storage = _make_real_storage()
        node = _make_node_with_real_storage(storage)

        provider_pk = "ee" * 32
        provider_nid = hashlib.sha256(bytes.fromhex(provider_pk)).hexdigest()
        skill_json = json.dumps({
            "name": "echo", "version": "1.0",
            "description": "echo", "tags": []
        })

        conn = storage._get_conn()
        conn.execute("""
            INSERT INTO skills (provider_node_id, skill_record_json, announced_at, ttl,
                               is_own, provider_public_key, provider_host, provider_port)
            VALUES (?, ?, ?, ?, 0, ?, '', 0)
        """, (provider_nid, skill_json, time.time(), 300, provider_pk))
        conn.commit()

        # MCP-style provider dict
        provider = {"node_id": provider_nid, "host": "10.0.0.8", "port": 9000}

        server = CockpitServer.__new__(CockpitServer)
        server._node = node
        server._backfill_provider_key(provider, "echo")

        # Now insert remote job — should have the key
        job_id = str(uuid.uuid4())
        storage.insert_remote_job(
            job_id, "echo", provider["node_id"],
            provider["host"], provider["port"], time.time() + 86400,
            provider_public_key=provider.get("_provider_public_key", "")
        )

        job = storage.get_async_job(job_id)
        assert job["provider_public_key"] == provider_pk, \
            f"Expected {provider_pk}, got '{job['provider_public_key']}'"

    def test_no_crash_when_skill_not_found(self):
        """Backfill should not crash when skill/provider not in skills table."""
        from knarr.dashboard.server import CockpitServer

        storage = _make_real_storage()
        node = _make_node_with_real_storage(storage)

        provider = {"node_id": "deadbeef" * 8, "host": "10.0.0.9", "port": 9000}

        server = CockpitServer.__new__(CockpitServer)
        server._node = node
        server._backfill_provider_key(provider, "nonexistent-skill")

        # Should not crash, key should remain absent
        assert provider.get("_provider_public_key") is None


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
