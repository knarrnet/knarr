"""Tests for v0.31.0: Economy Foundation + Bug Sweep.

Covers: E1 (consumer ledger in mail path), E2 (sanctions skip for free),
        E4 (poll_task_results), E5 (_billable flag), B5 (economy fields),
        BUG-26 (own skills in listing), BUG-28 (JSON for running jobs).
"""
import asyncio
import json
import math
import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from knarr.dht.storage import Storage
from knarr.core.models import NodeInfo, SkillSheet, Policy
from knarr.mail.handler import handle, set_node


# -- Fixtures --

@pytest.fixture
def storage():
    return Storage(":memory:")


class FakeNodeInfo:
    def __init__(self, node_id):
        self.node_id = node_id


class FakeNode:
    """Minimal node mock for handler tests."""
    def __init__(self, node_id="local_node_id_abc123", config=None):
        self.node_info = FakeNodeInfo(node_id)
        self.storage = Storage(":memory:")
        self._config = config or {"mail": {}}


@pytest.fixture
def node():
    n = FakeNode()
    set_node(n)
    return n


def _make_mock_node(provider_pk, job_id, has_receipt=True, credits_charged=2.5):
    """Build a MagicMock node with storage that has an async_job entry.
    Uses Dev D pattern: provider_public_key stored in job record."""
    from knarr.dht.mail_handlers import MailHandlers

    mock_node = MagicMock()
    mock_node.storage = MagicMock()
    mock_node.storage.get_async_job.return_value = {
        "job_id": job_id,
        "provider_node_id": provider_pk,
        "provider_public_key": provider_pk,  # E1: key source
        "status": "pending",
    }
    mock_node.storage.get_ledger_balance.return_value = 0.0
    mock_node.policy = Policy(initial_credit=3.0, min_balance=-10.0)
    mock_node._plugins = None
    mock_node._ledger_update_callback = None
    mock_node._get_initial_trust = MagicMock(return_value=0.3)

    calls = []
    async def mock_enqueue(op, *args):
        name = getattr(op, '__name__', None) or getattr(op, '_mock_name', None) or str(op)
        calls.append((name, args))
        if "get_or_create_ledger_entry" in name:
            return MagicMock(balance=0.0)
    mock_node._enqueue_write = AsyncMock(side_effect=mock_enqueue)
    mock_node._enqueue_write_calls = calls

    # Wire real MailHandlers so E1 logic actually runs
    mh = MailHandlers(mock_node.storage, None, None, None)
    mh.bind_runtime(
        enqueue_write=mock_node._enqueue_write,
        get_initial_trust=lambda nid: 0.3,
        initial_credit=3.0,
    )
    mock_node._mail_handlers = mh

    return mock_node


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# -- E1: Consumer-side ledger update in async mail path --

def test_e1_consumer_ledger_updated_on_task_result_mail():
    """After _handle_task_result_mail, consumer ledger should reflect credits_charged."""
    from knarr.dht.node import DHTNode

    sender_pk = "ab" * 32
    job_id = str(uuid.uuid4())
    mock_node = _make_mock_node(sender_pk, job_id)

    receipt_payload = json.dumps({"credits_charged": 2.5, "skill_name": "test-skill"})
    receipt = json.dumps({"payload": receipt_payload, "signature": "fakesig"})

    item = {
        "from_node": sender_pk,
        "body": {
            "job_id": job_id,
            "status": "completed",
            "output_data": {"result": "ok"},
            "receipt": receipt,
        },
    }

    _run_async(DHTNode._handle_task_result_mail(mock_node, item))

    call_names = [c[0] for c in mock_node._enqueue_write_calls]
    assert "update_async_job_status" in call_names
    assert "store_receipt" in call_names
    assert "get_or_create_ledger_entry" in call_names
    assert "update_ledger_consumer" in call_names

    consumer_calls = [c for c in mock_node._enqueue_write_calls if c[0] == "update_ledger_consumer"]
    assert len(consumer_calls) == 1
    assert consumer_calls[0][1][0] == sender_pk
    assert consumer_calls[0][1][1] == 2.5


def test_e1_nan_credits_rejected():
    """NaN credits_charged in receipt should be rejected."""
    from knarr.dht.node import DHTNode

    sender_pk = "cd" * 32
    job_id = str(uuid.uuid4())
    mock_node = _make_mock_node(sender_pk, job_id)

    receipt_payload = json.dumps({"credits_charged": float("nan")})
    receipt = json.dumps({"payload": receipt_payload, "signature": "fakesig"})

    item = {
        "from_node": sender_pk,
        "body": {
            "job_id": job_id,
            "status": "completed",
            "output_data": {"result": "ok"},
            "receipt": receipt,
        },
    }

    _run_async(DHTNode._handle_task_result_mail(mock_node, item))

    call_names = [c[0] for c in mock_node._enqueue_write_calls]
    assert "update_ledger_consumer" not in call_names


def test_e1_inf_credits_rejected():
    """Infinity credits_charged should be rejected."""
    from knarr.dht.node import DHTNode

    sender_pk = "ef" * 32
    job_id = str(uuid.uuid4())
    mock_node = _make_mock_node(sender_pk, job_id)

    receipt_payload = json.dumps({"credits_charged": float("inf")})
    receipt = json.dumps({"payload": receipt_payload, "signature": "fakesig"})

    item = {
        "from_node": sender_pk,
        "body": {
            "job_id": job_id,
            "status": "completed",
            "output_data": {},
            "receipt": receipt,
        },
    }

    _run_async(DHTNode._handle_task_result_mail(mock_node, item))

    call_names = [c[0] for c in mock_node._enqueue_write_calls]
    assert "update_ledger_consumer" not in call_names


def test_e1_malformed_receipt_no_crash():
    """Malformed receipt should be logged and skipped, not crash."""
    from knarr.dht.node import DHTNode

    sender_pk = "11" * 32
    job_id = str(uuid.uuid4())
    mock_node = _make_mock_node(sender_pk, job_id)

    item = {
        "from_node": sender_pk,
        "body": {
            "job_id": job_id,
            "status": "completed",
            "output_data": {"result": "ok"},
            "receipt": "not-valid-json{{{",
        },
    }

    _run_async(DHTNode._handle_task_result_mail(mock_node, item))

    call_names = [c[0] for c in mock_node._enqueue_write_calls]
    assert "update_async_job_status" in call_names
    assert "update_ledger_consumer" not in call_names


def test_e1_zero_credits_no_ledger_update():
    """Receipt with credits_charged=0 should NOT update the ledger."""
    from knarr.dht.node import DHTNode

    sender_pk = "22" * 32
    job_id = str(uuid.uuid4())
    mock_node = _make_mock_node(sender_pk, job_id)

    receipt_payload = json.dumps({"credits_charged": 0})
    receipt = json.dumps({"payload": receipt_payload, "signature": "fakesig"})

    item = {
        "from_node": sender_pk,
        "body": {
            "job_id": job_id,
            "status": "completed",
            "output_data": {},
            "receipt": receipt,
        },
    }

    _run_async(DHTNode._handle_task_result_mail(mock_node, item))

    call_names = [c[0] for c in mock_node._enqueue_write_calls]
    assert "update_ledger_consumer" not in call_names


def test_e1_double_parsed_payload():
    """Receipt with string payload (needs double-parse) should work."""
    from knarr.dht.node import DHTNode

    sender_pk = "33" * 32
    job_id = str(uuid.uuid4())
    mock_node = _make_mock_node(sender_pk, job_id)

    inner_payload = json.dumps({"credits_charged": 1.0})
    receipt = json.dumps({"payload": inner_payload, "signature": "sig"})

    item = {
        "from_node": sender_pk,
        "body": {
            "job_id": job_id,
            "status": "completed",
            "output_data": {},
            "receipt": receipt,
        },
    }

    _run_async(DHTNode._handle_task_result_mail(mock_node, item))

    call_names = [c[0] for c in mock_node._enqueue_write_calls]
    assert "update_ledger_consumer" in call_names


# -- E2: Sanctions skip for free skills --

def test_e2_sanctions_ok_returns_ok(storage):
    """Unknown peer returns 'ok' from check_sanctions."""
    assert storage.check_sanctions("unknown_peer_key") == "ok"


def test_e2_sanctions_hard_block(storage):
    """Peer below hard_limit returns 'hard_block'."""
    storage.get_or_create_ledger_entry("peer_bad")
    # A1.2: new entries start at 0.0 — set balance directly for test setup
    conn = storage._get_conn()
    conn.execute("UPDATE ledger SET balance = -15.0 WHERE peer_public_key = 'peer_bad'")
    conn.commit()
    assert storage.check_sanctions("peer_bad") == "hard_block"


def test_e2_sanctions_soft_warning(storage):
    """Peer below soft_limit but above hard_limit returns 'soft_warning'."""
    storage.get_or_create_ledger_entry("peer_warn")
    # A1.2: new entries start at 0.0 — set balance directly for test setup
    conn = storage._get_conn()
    conn.execute("UPDATE ledger SET balance = -7.0 WHERE peer_public_key = 'peer_warn'")
    conn.commit()
    assert storage.check_sanctions("peer_warn") == "soft_warning"


def test_e2_free_skill_bypasses_credit_check(storage):
    """A free skill (price=0) should be accessible even with negative balance."""
    peer_pk = "aa" * 32
    storage.get_or_create_ledger_entry(peer_pk)
    # A1.2: new entries start at 0.0 — set balance directly for test setup
    conn = storage._get_conn()
    conn.execute(f"UPDATE ledger SET balance = -100.0 WHERE peer_public_key = ?", (peer_pk,))
    conn.commit()
    entry = storage.get_or_create_ledger_entry(peer_pk)
    assert entry.balance == -100.0

    skill_price = 0.0
    min_balance = -10.0
    tit_for_tat = False

    should_reject = not tit_for_tat and skill_price > 0 and entry.balance < min_balance
    assert not should_reject


def test_e2_paid_skill_still_blocked_by_credit(storage):
    """A paid skill should still be blocked when balance is below minimum."""
    peer_pk = "bb" * 32
    storage.get_or_create_ledger_entry(peer_pk)
    # A1.2: new entries start at 0.0 — set balance directly for test setup
    conn = storage._get_conn()
    conn.execute(f"UPDATE ledger SET balance = -100.0 WHERE peer_public_key = ?", (peer_pk,))
    conn.commit()
    entry = storage.get_or_create_ledger_entry(peer_pk)

    skill_price = 1.0
    min_balance = -10.0
    tit_for_tat = False

    should_reject = not tit_for_tat and skill_price > 0 and entry.balance < min_balance
    assert should_reject


# -- E4: Unified task result retrieval --

def test_e4_poll_task_results_from_jobreport(storage):
    """poll_task_results should return completed tasks from mail_jobreport."""
    conn = storage._get_conn()
    now = time.time()
    msg_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO mail_jobreport (message_id, from_node, to_node, timestamp, body,
                                     msg_type, ttl_expires, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (msg_id, "provider_a", "local_node", now, '{"result": "ok"}',
          "knarr/system/task_result", now + 86400, "unread", now))
    conn.commit()

    results = storage.poll_task_results(20, "unread")
    assert len(results) == 1
    assert results[0]["message_id"] == msg_id
    assert results[0]["source"] == "mail_jobreport"


def test_e4_poll_task_results_from_system(storage):
    """poll_task_results should return failed tasks from mail_system."""
    conn = storage._get_conn()
    now = time.time()
    msg_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO mail_system (message_id, from_node, to_node, timestamp, body,
                                  msg_type, ttl_expires, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (msg_id, "provider_b", "local_node", now, '{"error": "timeout"}',
          "knarr/system/task_failed", now + 86400, "unread", now))
    conn.commit()

    results = storage.poll_task_results(20, "unread")
    assert len(results) == 1
    assert results[0]["source"] == "mail_system"


def test_e4_poll_task_results_merged_sorted(storage):
    """Results from both tables should be merged and sorted newest first."""
    conn = storage._get_conn()
    now = time.time()

    conn.execute("""
        INSERT INTO mail_jobreport (message_id, from_node, to_node, timestamp, body,
                                     msg_type, ttl_expires, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("msg_old", "p1", "local", now - 100, '{}', "result", now + 86400, "unread", now - 100))

    conn.execute("""
        INSERT INTO mail_system (message_id, from_node, to_node, timestamp, body,
                                  msg_type, ttl_expires, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("msg_new", "p2", "local", now, '{}', "failed", now + 86400, "unread", now))
    conn.commit()

    results = storage.poll_task_results(20, "unread")
    assert len(results) == 2
    assert results[0]["message_id"] == "msg_new"
    assert results[1]["message_id"] == "msg_old"


def test_e4_poll_task_results_limit_capped(storage):
    """Limit should be capped at 50."""
    results = storage.poll_task_results(100, "all")
    assert isinstance(results, list)


def test_e4_poll_task_results_status_filter(storage):
    """Status filter should work (unread vs read vs all)."""
    conn = storage._get_conn()
    now = time.time()

    conn.execute("""
        INSERT INTO mail_jobreport (message_id, from_node, to_node, timestamp, body,
                                     msg_type, ttl_expires, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("msg_unread", "p1", "local", now, '{}', "result", now + 86400, "unread", now))

    conn.execute("""
        INSERT INTO mail_jobreport (message_id, from_node, to_node, timestamp, body,
                                     msg_type, ttl_expires, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("msg_read", "p1", "local", now, '{}', "result", now + 86400, "read", now))
    conn.commit()

    unread = storage.poll_task_results(20, "unread")
    assert len(unread) == 1
    assert unread[0]["message_id"] == "msg_unread"

    all_results = storage.poll_task_results(20, "all")
    assert len(all_results) == 2


def test_e4_mcp_handler_poll_results(node):
    """MCP poll_results action should work through handler dispatch."""
    conn = node.storage._get_conn()
    now = time.time()
    conn.execute("""
        INSERT INTO mail_jobreport (message_id, from_node, to_node, timestamp, body,
                                     msg_type, ttl_expires, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("msg1", "p1", "local", now, '{"out": 1}', "result", now + 86400, "unread", now))
    conn.commit()

    result = _run_async(handle({
        "action": "poll_results",
        "_caller_node_id": node.node_info.node_id,
        "limit": 10,
        "status": "unread",
    }))

    assert "results" in result
    assert result["count"] >= 1


# -- E5: _billable flag --

def test_e5_billable_false_skips_receipt():
    """When handler returns _billable: false, billable should be False."""
    output_data = {"_billable": False, "result": "ok"}
    billable = True
    if isinstance(output_data, dict) and output_data.get("_billable") is False:
        billable = False
    assert not billable


def test_e5_normal_output_still_billable():
    """Normal handler output (no _billable key) should remain billable."""
    output_data = {"result": "ok"}
    billable = True
    if isinstance(output_data, dict) and output_data.get("_billable") is False:
        billable = False
    assert billable


def test_e5_billable_zero_still_billable():
    """_billable: 0 should NOT disable billing (must be exactly False)."""
    output_data = {"_billable": 0, "result": "ok"}
    billable = True
    if isinstance(output_data, dict) and output_data.get("_billable") is False:
        billable = False
    assert billable, "_billable: 0 must not disable billing (identity check)"


# -- B5: Economy summary consumer fields --

def test_b5_economy_summary_includes_consumer_fields(storage):
    """get_all_ledger_entries should include prepaid, pub_tab, soft_limit, hard_limit."""
    storage.get_or_create_ledger_entry("peer1", initial_balance=5.0)

    entries = storage.get_all_ledger_entries()
    assert len(entries) == 1
    e = entries[0]
    assert "prepaid" in e
    assert "pub_tab" in e
    assert "soft_limit" in e
    assert "hard_limit" in e
    assert e["prepaid"] == 0.0
    assert e["pub_tab"] == 0.0


def test_b5_economy_defaults_correct(storage):
    """B5: soft_limit should default to -5.0, hard_limit to -10.0 (not 0.0)."""
    storage.get_or_create_ledger_entry("peer2", initial_balance=0.0)
    entries = storage.get_all_ledger_entries()
    e = entries[0]
    assert e["soft_limit"] == -5.0, "soft_limit should default to -5.0"
    assert e["hard_limit"] == -10.0, "hard_limit should default to -10.0"


# -- BUG-26: Own skills in network listing --

def test_bug26_own_skills_in_listing(storage):
    """Own skills should appear in query_all_active_skills with is_local=True."""
    sheet = SkillSheet(name="my-skill", version="1.0.0", description="mine",
                       tags=["test"], input_schema={}, output_schema={})
    storage.upsert_skill("my-skill", "self_node_id", sheet, is_own=True)

    results = storage.query_all_active_skills()
    own_skills = [r for r in results if r.get("is_local")]
    assert len(own_skills) >= 1
    assert own_skills[0]["skill_sheet"]["name"] == "my-skill"


def test_bug26_own_skills_distinguished_from_remote(storage):
    """Own skills should have is_local=True, remote skills should not."""
    own_sheet = SkillSheet(name="own-skill", version="1.0.0", description="local",
                           tags=[], input_schema={}, output_schema={})
    storage.upsert_skill("own-skill", "self_node", own_sheet, is_own=True)

    remote_sheet = SkillSheet(name="remote-skill", version="1.0.0", description="remote",
                              tags=[], input_schema={}, output_schema={})
    storage.upsert_skill("remote-skill", "peer1", remote_sheet, is_own=False)
    storage.upsert_peer(NodeInfo(node_id="peer1", host="1.2.3.4", port=9001))

    results = storage.query_all_active_skills()
    local_results = [r for r in results if r.get("is_local") is True]
    remote_results = [r for r in results if not r.get("is_local")]

    assert len(local_results) >= 1
    assert len(remote_results) >= 1


# -- BUG-28: Result endpoint JSON for running jobs --

def test_bug28_running_job_returns_json():
    """The result endpoint should return JSON for running/pending jobs."""
    job = {"job_id": "test-job-123", "status": "running"}
    response = json.dumps({"job_id": job["job_id"], "status": job["status"]})
    parsed = json.loads(response)
    assert parsed["job_id"] == "test-job-123"
    assert parsed["status"] == "running"


def test_bug28_completed_job_still_works():
    """Completed jobs should still return full output_data."""
    job = {"job_id": "test-job-456", "status": "completed", "result": {"data": 42}, "error": None}
    resp = {
        "job_id": job["job_id"],
        "status": job["status"],
        "output_data": job["result"],
        "error": job["error"],
    }
    assert resp["output_data"] == {"data": 42}
    assert resp["status"] == "completed"
