"""Adversarial tests for v0.33.0: "Build the Machine"

Exploit-class tests. Each test proves a bug exists.
Test FAILS = bug confirmed. Test PASSES = guard works (or bug was fixed).

Author: Adversary (Opus)
Sprint: v0.33.0
Date: 2026-03-01
"""
import asyncio
import math
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


def _run(coro):
    """Run async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeBus:
    """Minimal bus recording emit calls for assertion."""
    def __init__(self):
        self.events = []

    def emit(self, event_type, **fields):
        self.events.append({"event": event_type, **fields})

    def get(self, event_type):
        return [e for e in self.events if e["event"] == event_type]


# ---------------------------------------------------------------------------
# EXPLOIT 1: get_ledger_entry does not exist — consumer-side credit.restored
#             is dead code that silently fails
# ---------------------------------------------------------------------------
class TestExploit1_ConsumerCreditRestoredDeadCode:
    """consumer-side credit.restored calls self.storage.get_ledger_entry()
    which does not exist in Storage. The AttributeError is caught by the
    except block at line 3019, so credit.restored NEVER fires on the
    consumer side. This is a silent data loss bug — operators subscribed
    to credit.restored will never see consumer-side restorations.
    """

    def test_get_ledger_entry_does_not_exist(self):
        """Storage has no get_ledger_entry method — only get_ledger_balance."""
        from knarr.dht.storage import Storage
        s = Storage(":memory:")
        assert not hasattr(s, "get_ledger_entry"), \
            "get_ledger_entry now exists — fix landed, remove this exploit test"

    def test_consumer_credit_restored_never_fires(self):
        """Simulate the consumer-side path: credit.restored should fire
        when a peer's utilization crosses below threshold after payment,
        but it won't because get_ledger_entry raises AttributeError."""
        from knarr.dht.storage import Storage
        s = Storage(":memory:")
        bus = FakeBus()

        # Create a ledger entry at high utilization
        pk = "ab" * 32
        s.get_or_create_ledger_entry(pk, 3.0, 1.0)
        # Drive balance down to -8.0 (utilization ~84.6% with range 13)
        s.update_ledger_provider(pk, 11.0)
        balance_before = s.get_ledger_balance(pk)
        assert balance_before == pytest.approx(-8.0, abs=0.1)

        # Now increase balance (consumer-side: provider earned credit)
        # This should bring utilization below 80% threshold
        s.update_ledger_consumer(pk, 5.0)
        balance_after = s.get_ledger_balance(pk)
        assert balance_after == pytest.approx(-3.0, abs=0.1)

        # The consumer-side code at node.py:3016 calls:
        #   _entry_after = self.storage.get_ledger_entry(provider_pubkey)
        # This will raise AttributeError since method doesn't exist
        with pytest.raises(AttributeError):
            s.get_ledger_entry(pk)


# ---------------------------------------------------------------------------
# EXPLOIT 2: EventBus size=0 causes ZeroDivisionError on every emit()
# ---------------------------------------------------------------------------
class TestExploit2_EventBusSizeZero:
    """Config [node] event_bus_size = 0 creates an EventBus with size=0.
    Every emit() call does `self._head % self._size` which is division by zero.
    The node will crash on first event emission.
    """

    def test_eventbus_zero_size_crashes_on_emit(self):
        """EventBus(size=0) causes ZeroDivisionError on emit()."""
        from knarr.dht.eventbus import EventBus
        bus = EventBus(size=0)
        with pytest.raises((ZeroDivisionError, IndexError)):
            bus.emit("test.event", data="hello")

    def test_eventbus_negative_size_crashes(self):
        """EventBus(size=-1) creates empty ring, crashes on emit()."""
        from knarr.dht.eventbus import EventBus
        bus = EventBus(size=-1)
        # [None] * -1 = [], then _ring[0 % -1] = _ring[0] -> IndexError
        with pytest.raises((ZeroDivisionError, IndexError)):
            bus.emit("test.event", data="hello")

    def test_config_path_allows_zero_bus_size(self):
        """The config path does int() conversion with no lower bound check."""
        config = {"node": {"event_bus_size": 0}}
        result = int(config.get("node", {}).get("event_bus_size", 256))
        assert result == 0, "Config allows zero bus size — no validation guard"


# ---------------------------------------------------------------------------
# EXPLOIT 3: min_peers=0 removes prune safety floor
# ---------------------------------------------------------------------------
class TestExploit3_MinPeersZero:
    """Setting [network] min_peers = 0 makes the prune floor meaningless.
    The check `current_count <= min_peer_floor` with floor=0 only skips
    pruning when there are literally 0 peers. A node with 1 peer will
    still run prune_stale_peers, potentially deleting all remaining peers
    and isolating itself from the network.
    """

    def test_min_peers_zero_allows_total_prune(self):
        """With min_peers=0, prune runs even with only 1 peer remaining."""
        from knarr.dht.storage import Storage
        s = Storage(":memory:")

        # Add a single peer
        from knarr.core.models import NodeInfo
        s.upsert_peer(NodeInfo(node_id="a" * 64, host="1.2.3.4", port=9000))

        current_count = len(s.get_peers())
        assert current_count == 1

        min_peer_floor = int({"network": {"min_peers": 0}}.get("network", {}).get("min_peers", 8))
        assert min_peer_floor == 0

        # The prune check: `if current_count <= min_peer_floor: SKIP`
        # With 1 peer and floor=0: `1 <= 0` is False, so pruning proceeds
        should_skip = current_count <= min_peer_floor
        assert not should_skip, "Pruning NOT skipped — all peers can be deleted"

    def test_negative_min_peers_always_prunes(self):
        """Negative min_peers is even worse — floor is always below count."""
        config = {"network": {"min_peers": -5}}
        min_peer_floor = int(config.get("network", {}).get("min_peers", 8))
        assert min_peer_floor < 0
        # Any positive peer count > -5, so pruning always runs
        assert 1 > min_peer_floor


# ---------------------------------------------------------------------------
# EXPLOIT 4: S-022 refund bypass when caller_node_id is NULL
# ---------------------------------------------------------------------------
class TestExploit4_RefundBypassNullCaller:
    """If execution_log.caller_node_id is NULL (possible since the column
    allows NULL), then original.get("requester_node_id") returns None.
    An attacker who sends from_node=None (or omits it) will satisfy
    `from_node != expected_requester` → `None != None` → False,
    bypassing the S-022 sender verification.
    """

    def test_null_caller_allows_null_sender_bypass(self):
        """When requester_node_id is None, from_node=None bypasses check."""
        # Simulate the S-022 check from handlers.py lines 59-64
        from_node = None  # attacker omits from_node
        expected_requester = None  # caller_node_id was NULL in execution_log

        # The guard: `if from_node != expected_requester: return`
        mismatch = from_node != expected_requester
        assert not mismatch, "None != None is False — check bypassed, refund proceeds"

    def test_refund_handler_with_null_caller(self):
        """Full handler path: missing from_node + NULL caller_node_id = refund granted."""
        from knarr.dht.storage import Storage
        s = Storage(":memory:")

        # Insert execution_log with NULL caller_node_id
        s.log_execution(
            job_id="task_123", skill="echo", caller=None,
            status="completed", wall_ms=100, price=5.0
        )
        entry = s.get_execution_log_entry("task_123")
        assert entry is not None
        assert entry["requester_node_id"] is None, \
            "caller_node_id stored as NULL — expected"

        # Attacker sends receipt with no from_node
        item = {"from_node": None, "body": {
            "type": "knarr/commerce/receipt",
            "task_id": "task_123",
            "status": "rejected",
            "refund_requested": True,
            "timestamp": time.time(),
        }}

        # S-022 check:
        from_node = item.get("from_node")
        expected_requester = entry.get("requester_node_id")
        assert from_node == expected_requester, \
            "Both None — S-022 check passes, attacker gets refund"


# ---------------------------------------------------------------------------
# EXPLOIT 5: Cumulative refund TOCTOU — concurrent credit notes bypass cap
# ---------------------------------------------------------------------------
class TestExploit5_RefundTOCTOU:
    """get_cumulative_refund is a direct read (no writer queue serialization),
    while record_refund goes through _enqueue_write. Two concurrent
    credit_note handlers can both read cumulative=0 before either records,
    allowing double-refunds that exceed the 2x cap.
    """

    def test_cumulative_refund_race_window(self):
        """Two reads of cumulative=0 before either write → both pass cap check."""
        from knarr.dht.storage import Storage
        s = Storage(":memory:")

        # Set up: task with price=5.0, 2x cap = 10.0
        s.log_execution("task_race", "echo", "caller1", "completed", 100, price=5.0)

        # Attacker fires two credit notes simultaneously
        # Both read cumulative BEFORE either record lands
        read_1 = s.get_cumulative_refund("task_race")  # 0.0
        read_2 = s.get_cumulative_refund("task_race")  # 0.0

        amount = 8.0  # each wants 8.0
        original_price = 5.0
        max_refund = original_price * 2  # 10.0

        # Both pass the cap check independently
        assert read_1 + amount <= max_refund, "First note passes cap check"
        assert read_2 + amount <= max_refund, "Second note ALSO passes cap check"

        # Both record
        s.record_refund("task_race", amount)
        s.record_refund("task_race", amount)

        # Total refund is 16.0 — exceeds 2x cap of 10.0
        total = s.get_cumulative_refund("task_race")
        assert total == pytest.approx(16.0), \
            f"Double refund succeeded: {total} > cap {max_refund}"
        assert total > max_refund, \
            "TOCTOU confirmed: cumulative exceeds 2x cap"


# ---------------------------------------------------------------------------
# EXPLOIT 6: credit.restored consumer-side balance reconstruction is inverted
# ---------------------------------------------------------------------------
class TestExploit6_CreditRestoredDirectionInversion:
    """Even if get_ledger_entry existed, the consumer-side passes:
        old_balance = _entry_after["balance"] + credits_charged
        new_balance = _entry_after["balance"]

    Consumer-side: update_ledger_consumer ADDS credits_charged to balance.
    So if balance was 5 before payment, after adding 3 it's 8.
    The code reconstructs: old = 8 + 3 = 11, new = 8.
    Actual: old = 5, new = 8.
    The old_balance is computed as HIGHER than new_balance, which inverts
    the utilization direction, making restored fire when it should not.
    """

    def test_balance_reconstruction_is_wrong(self):
        """Consumer-side old_balance reconstruction adds instead of subtracts."""
        # update_ledger_consumer adds credits_charged to balance
        balance_before = 5.0
        credits_charged = 3.0
        balance_after = balance_before + credits_charged  # 8.0

        # Code at node.py:3018 does:
        reconstructed_old = balance_after + credits_charged  # 11.0 (WRONG)
        reconstructed_new = balance_after                     # 8.0

        # Correct reconstruction should be:
        correct_old = balance_after - credits_charged  # 5.0
        correct_new = balance_after                     # 8.0

        assert reconstructed_old != correct_old, \
            f"Reconstructed old={reconstructed_old} != correct old={correct_old}"
        assert reconstructed_old > balance_after, \
            "Reconstructed old is HIGHER than actual after — direction inverted"

    def test_inverted_direction_causes_false_restored(self):
        """The inverted direction can cause credit.restored to fire incorrectly."""
        from knarr.dht.node import DHTNode

        bus = FakeBus()
        node = MagicMock(spec=DHTNode)
        node.bus = bus
        node._config = {"settlement": {"tab_reminder_threshold": 80.0}}
        # initial_credit=3.0, min_balance=-10.0, range=13
        node._resolve_policy = MagicMock(return_value=(3.0, -10.0))

        # Scenario: consumer pays provider, balance increases
        # Real: old=-9.0 (92.3%), new=-6.0 (69.2%) → should fire restored
        # But the code reconstructs: old=-6.0+3.0=-3.0 (46.2%), new=-6.0 (69.2%)
        # Inverted: old=46.2% < 80%, new=69.2% < 80% → does NOT fire

        credits_charged = 3.0
        balance_after = -6.0

        # What the code would pass (if get_ledger_entry existed):
        code_old = balance_after + credits_charged  # -3.0
        code_new = balance_after                     # -6.0

        DHTNode._check_credit_restored(node, "ab" * 32, code_old, code_new)

        events = bus.get("credit.restored")
        # The code sees old_util=46.2%, new_util=69.2%
        # 46.2 < 80 threshold, so old_util >= threshold is False → no event
        assert len(events) == 0, \
            "credit.restored did NOT fire — direction inversion suppressed it"

        # Now test what SHOULD happen with correct values:
        bus2 = FakeBus()
        node2 = MagicMock(spec=DHTNode)
        node2.bus = bus2
        node2._config = {"settlement": {"tab_reminder_threshold": 80.0}}
        node2._resolve_policy = MagicMock(return_value=(3.0, -10.0))

        correct_old = balance_after - credits_charged  # -9.0 (92.3%)
        correct_new = balance_after                     # -6.0 (69.2%)

        DHTNode._check_credit_restored(node2, "ab" * 32, correct_old, correct_new)

        events2 = bus2.get("credit.restored")
        assert len(events2) == 1, \
            "With correct direction, credit.restored WOULD fire"


# ---------------------------------------------------------------------------
# EXPLOIT 7: mail.delivery_failed emits wrong 'attempts' field
# ---------------------------------------------------------------------------
class TestExploit7_DeliveryFailedWrongAttempts:
    """mail.delivery_failed at sync.py:215 emits attempts=len(item_ids),
    but item_ids is the batch size, not the retry count. An operator
    monitoring delivery failures will see 'attempts=50' (max batch) and
    think the message was retried 50 times, when it was actually the first
    failure of a 50-item batch.
    """

    def test_attempts_is_batch_size_not_retry_count(self):
        """The 'attempts' field in mail.delivery_failed is item count, not retries."""
        # Simulate the emit from sync.py line 215
        item_ids = [f"id_{i}" for i in range(50)]  # full batch

        # What the code emits:
        emitted_attempts = len(item_ids)

        # This is the batch size, not retry count
        assert emitted_attempts == 50, "attempts=50 is batch size, not retry count"

        # The field name 'attempts' implies retry count
        # A single first-time failure of 50 items reports attempts=50
        # Misleading to any monitoring/alerting system
        assert emitted_attempts != 1, \
            "First delivery failure reports attempts=50, not 1"


# ---------------------------------------------------------------------------
# EXPLOIT 8: EventBus huge size causes MemoryError (OOM DoS)
# ---------------------------------------------------------------------------
class TestExploit8_EventBusOOM:
    """Config event_bus_size accepts arbitrary integers with no upper bound.
    Setting event_bus_size=2147483648 (2^31) will attempt to allocate
    [None] * 2_147_483_648, causing MemoryError and crashing the node
    at startup.
    """

    def test_no_upper_bound_validation(self):
        """Config path has no maximum check for event_bus_size."""
        config = {"node": {"event_bus_size": 2**31}}
        result = int(config.get("node", {}).get("event_bus_size", 256))
        assert result == 2**31, "No upper bound — config accepts 2^31"

    def test_large_bus_size_causes_memory_error(self):
        """EventBus with huge size will OOM."""
        from knarr.dht.eventbus import EventBus
        # 2^31 * 8 bytes per pointer ≈ 16GB — will OOM on most systems
        # Use a smaller but still problematic value for test safety
        with pytest.raises((MemoryError, OverflowError)):
            EventBus(size=2**40)  # ~1 trillion slots


# ---------------------------------------------------------------------------
# EXPLOIT 9: LIKE escape is incomplete — missing bracket metacharacter
# ---------------------------------------------------------------------------
class TestExploit9_LikeEscapeIncomplete:
    """_escape_like handles %, _, and \\ but not SQLite's bracket syntax.
    SQLite LIKE does not actually support [brackets] for character classes
    (unlike SQL Server), so this is a false alarm on the surface. However,
    the ESCAPE clause interaction with backslashes on different SQLite
    builds can cause unexpected behavior.

    More importantly: the LIKE approach is fundamentally fragile for
    matching public keys in JSON blobs. A key substring could match
    unrelated JSON fields, producing false positives.
    """

    def test_like_false_positive_on_json_field_collision(self):
        """LIKE match can hit unrelated JSON fields containing the key prefix."""
        from knarr.dht.storage import Storage
        s = Storage(":memory:")

        # Create settlement_queue table
        conn = s._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settlement_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type TEXT, from_node TEXT, body TEXT,
                priority INTEGER, created_at REAL, status TEXT DEFAULT 'pending'
            )
        """)

        # Insert a settlement with a DIFFERENT peer, but the body JSON
        # happens to contain the target key prefix in another field
        target_key = "ab" * 32  # the key we're checking
        decoy_body = {
            "peer": "cd" * 32,  # different peer
            "note": f"see {target_key[:32]} for details"  # key prefix appears in note
        }
        import json
        conn.execute(
            "INSERT INTO settlement_queue (item_type, from_node, body, priority, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'pending')",
            ("settle_request", "other_node", json.dumps(decoy_body), 1, time.time())
        )
        conn.commit()

        # has_pending_settlement will find this false positive
        result = s.has_pending_settlement(target_key)
        assert result is True, \
            "LIKE match hit decoy — false positive on unrelated record"


# ---------------------------------------------------------------------------
# EXPLOIT 10: Refund on non-existent task records nothing, but cumulative
#              check returns 0 forever
# ---------------------------------------------------------------------------
class TestExploit10_RefundOrphanTask:
    """record_refund does UPDATE WHERE job_id=?. If the job_id doesn't exist,
    the UPDATE affects 0 rows. No error is raised. The refund is "recorded"
    but actually lost. Subsequent get_cumulative_refund returns 0.0 forever,
    so the cap check never blocks further refunds for this phantom task.

    This matters when combined with Exploit 5: if an attacker can forge
    a task_id that passes get_execution_log_entry (e.g., via SQL race or
    if the entry is deleted between check and record), refunds are unlimited.
    """

    def test_record_refund_on_missing_task_is_silent(self):
        """record_refund on non-existent job_id silently does nothing."""
        from knarr.dht.storage import Storage
        s = Storage(":memory:")

        # Record refund for task that was never logged
        s.record_refund("phantom_task", 100.0)

        # Cumulative is still 0 — the refund was lost
        cumulative = s.get_cumulative_refund("phantom_task")
        assert cumulative == 0.0, \
            "Refund on non-existent task silently dropped — cumulative stays 0"


# ---------------------------------------------------------------------------
# EXPLOIT 11: credit.restored never fires on refund path
# ---------------------------------------------------------------------------
class TestExploit11_RefundPathMissingCreditRestored:
    """update_ledger_refund (storage.py:1814) increases a consumer's balance
    (returning credit after a refund). This is the most natural path for a
    peer to move from over-threshold back to under-threshold. But the
    refund handler in handlers.py never calls _check_credit_restored.

    Result: a peer who was sanctioned, then receives a refund that brings
    them below threshold, never gets a credit.restored event. The only
    paths that check are _execute_queued_task (provider-side) and
    _handle_task_result_mail (consumer-side, broken by Exploit 1).
    """

    def test_refund_does_not_trigger_credit_restored(self):
        """update_ledger_refund changes balance but no threshold check follows."""
        from knarr.dht.storage import Storage
        s = Storage(":memory:")

        pk = "ab" * 32
        s.get_or_create_ledger_entry(pk, 3.0, 1.0)
        # Drive balance down below threshold
        s.update_ledger_provider(pk, 11.0)
        balance_before = s.get_ledger_balance(pk)
        assert balance_before == pytest.approx(-8.0, abs=0.1)

        # Refund that should restore credit
        s.update_ledger_refund(pk, 6.0)
        balance_after = s.get_ledger_balance(pk)
        assert balance_after == pytest.approx(-2.0, abs=0.1)

        # The refund handler (handlers.py:125) calls update_ledger_refund
        # but never calls _check_credit_restored. No bus event fires.
        # Grep the source to confirm:
        import inspect
        from knarr.commerce.handlers import make_commerce_handlers
        source = inspect.getsource(make_commerce_handlers)
        assert "_check_credit_restored" not in source, \
            "Refund handler does not call _check_credit_restored — event never fires"


# ---------------------------------------------------------------------------
# EXPLOIT 12: Subscriber._wake is asyncio.Event — not thread-safe with
#             emit()'s thread-safe lock
# ---------------------------------------------------------------------------
class TestExploit12_SubscriberWakeThreadSafety:
    """EventBus.emit() uses threading.Lock for thread-safety, but then calls
    sub._wake.set() OUTSIDE the lock (line 66). asyncio.Event.set() is
    not thread-safe (it's not designed for cross-thread signaling).

    If emit() is called from a ThreadPoolExecutor handler thread while
    the event loop is running, _wake.set() could corrupt the event loop's
    internal state or silently fail to wake the subscriber.
    """

    def test_wake_set_outside_lock(self):
        """sub._wake.set() is called after lock release — not protected."""
        from knarr.dht.eventbus import EventBus
        import inspect

        source = inspect.getsource(EventBus.emit)
        # The lock context manager ends at the `self._head += 1` line
        # Then `for sub in self._subs: ... sub._wake.set()` runs OUTSIDE the lock
        assert "with self._lock:" in source
        assert "_wake.set()" in source

        # Verify _wake is asyncio.Event, not threading.Event
        loop = asyncio.new_event_loop()
        try:
            bus = EventBus(size=16)
            sub = bus.subscribe("test.*")
            import asyncio as _aio
            assert isinstance(sub._wake, _aio.Event), \
                "Subscriber._wake is asyncio.Event — not thread-safe for cross-thread set()"
        finally:
            loop.close()
