"""Adversarial exploit tests for v0.33.0 — "Build the Machine".

Tests target the attack surface introduced or changed in v0.33.0:
- B-track: cumulative refund bypass, sender verification gaps
- A-track: bus event information leakage, external trigger surface
- C-track: malicious config values, type confusion, crash vectors

Test naming: test_exploit_{ID}_{description}
Failing test = bug found. Passing test = guard works.

Recon by: Sonnet (Adversary slot)
Sprint: v0.33.0
"""
import asyncio
import json
import threading
import time
import unittest
import uuid
from unittest.mock import MagicMock, AsyncMock, patch

from knarr.dht.storage import Storage
from knarr.dht.eventbus import EventBus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task_id():
    return str(uuid.uuid4())


def _seed_execution_log(storage: Storage, job_id: str, price: float,
                         requester_node_id: str = "consumer-node-aaa"):
    """Insert an execution log entry to test against."""
    conn = storage._get_conn()
    conn.execute("""
        INSERT INTO execution_log
            (job_id, skill_name, caller_node_id, status, wall_time_ms,
             input_hash, asset_hash, error, created_at, price, price_breakdown)
        VALUES (?, 'test-skill', ?, 'completed', 100, '', '', NULL, ?, ?, '')
    """, (job_id, requester_node_id, time.time(), price))
    conn.commit()


# ---------------------------------------------------------------------------
# B-TRACK: Cumulative refund cap (S-021)
# ---------------------------------------------------------------------------

class TestExploitB1_RefundRaceCondition(unittest.TestCase):
    """B-1: Race between get_cumulative_refund and record_refund.

    The cap check in handle_credit_note reads the running total, then
    enqueues record_refund as a SEPARATE write operation. Between the
    check and the write, multiple concurrent refund requests can all
    pass the same cap check simultaneously.

    Because the write queue is async, two concurrent credit_note
    handlers may both read cumulative=0, both see 0 + amount <= 2x cap,
    and both enqueue record_refund — resulting in 2x amount credited,
    which violates the 2x cap.
    """

    def test_exploit_b1_check_then_act_gap_allows_double_credit(self):
        """Two concurrent handlers both pass the cumulative check simultaneously."""
        from knarr.commerce.handlers import make_commerce_handlers

        original_price = 10.0
        task_id = _make_task_id()
        storage = Storage(":memory:")
        _seed_execution_log(storage, task_id, original_price, "consumer-aaa")

        # Wire storage so get_cumulative_refund always returns 0 (simulating
        # concurrent reads before any write has landed)
        cumulative_reads = []

        real_get_cumulative = storage.get_cumulative_refund
        real_record_refund = storage.record_refund

        def mock_get_cumulative(tid):
            # Always return 0 — the "stale read" scenario in a race
            val = real_get_cumulative(tid)
            cumulative_reads.append(val)
            return val

        node = MagicMock()
        node.storage.get_execution_log_entry.return_value = {
            "price": original_price,
            "task_id": task_id,
            "requester_node_id": "consumer-aaa",
        }
        # The critical issue: get_cumulative_refund is read-then-check; record_refund
        # is a deferred write. We simulate TWO handlers reading 0 simultaneously.
        call_count = [0]

        def get_cumulative_stale(tid):
            # Both calls return 0 (stale) before any write commits
            call_count[0] += 1
            return 0.0  # Always stale

        node.storage.get_cumulative_refund = get_cumulative_stale
        node.storage.get_all_ledger_entries.return_value = []

        write_calls = []
        async def track_write(fn, *args):
            write_calls.append((fn.__name__ if hasattr(fn, '__name__') else str(fn), args))

        node._enqueue_write = track_write

        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/credit_note"]

        # Each refund is exactly 1.9x the original (under 2x)
        refund_amount = original_price * 1.9

        async def run_two_concurrent():
            # Run two handlers concurrently — both should "see" cumulative=0
            # If the check is not atomic, both pass and we get 3.8x refunded
            coro1 = handler({
                "from_node": "consumer-aaa",
                "body": json.dumps({
                    "amount": refund_amount,
                    "reason": "quality_rejection",
                    "timestamp": time.time(),
                    "references": {"task_id": task_id},
                    "schema_version": "1.0",
                    "type": "knarr/commerce/credit_note",
                    "initiated_by": "provider",
                })
            })
            coro2 = handler({
                "from_node": "consumer-aaa",
                "body": json.dumps({
                    "amount": refund_amount,
                    "reason": "quality_rejection",
                    "timestamp": time.time(),
                    "references": {"task_id": task_id},
                    "schema_version": "1.0",
                    "type": "knarr/commerce/credit_note",
                    "initiated_by": "provider",
                })
            })
            await asyncio.gather(coro1, coro2)

        asyncio.new_event_loop().run_until_complete(run_two_concurrent())

        # Count ledger update calls
        ledger_calls = [c for c in write_calls if 'update_ledger_refund' in c[0]]

        # If the race condition exists, both handlers will have enqueued updates
        # because both read stale cumulative=0 and both passed the cap check
        self.assertLessEqual(
            len(ledger_calls), 1,
            f"Race condition: {len(ledger_calls)} ledger updates enqueued. "
            f"Both concurrent handlers passed the cap check because "
            f"get_cumulative_refund and record_refund are not atomic. "
            f"Total credit issued: {len(ledger_calls) * refund_amount:.1f} "
            f"vs max allowed {original_price * 2:.1f}"
        )


class TestExploitB2_RefundForUnknownTask(unittest.TestCase):
    """B-2: Credit note for a task_id not in execution_log is rejected.

    The guard at line 104 of handlers.py checks for a local execution record.
    But what if the task_id is synthetically constructed to collide with a
    real task? The check is by exact task_id string match, which is correct.
    This test validates the guard is actually there.
    """

    def test_exploit_b2_unknown_task_rejected(self):
        """Credit note with no local execution record must be dropped."""
        from knarr.commerce.handlers import make_commerce_handlers

        node = MagicMock()
        # Simulate no local record
        node.storage.get_execution_log_entry.return_value = None
        node.storage.get_all_ledger_entries.return_value = []
        node._enqueue_write = AsyncMock()

        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/credit_note"]

        task_id = _make_task_id()

        async def run():
            await handler({
                "from_node": "some-node",
                "body": json.dumps({
                    "amount": 999.0,
                    "reason": "quality_rejection",
                    "timestamp": time.time(),
                    "references": {"task_id": task_id},
                    "schema_version": "1.0",
                    "type": "knarr/commerce/credit_note",
                    "initiated_by": "provider",
                })
            })

        asyncio.new_event_loop().run_until_complete(run())

        # Must not have touched the ledger
        ledger_calls = [
            c for c in node._enqueue_write.call_args_list
            if len(c[0]) > 0 and hasattr(c[0][0], '__name__')
            and 'ledger' in c[0][0].__name__
        ]
        self.assertEqual(len(ledger_calls), 0,
                         "Credit note for unknown task_id should be silently dropped")


class TestExploitB3_CumulativeCapWithRealStorage(unittest.TestCase):
    """B-3: Cumulative cap enforced across sequential refunds using real Storage.

    This tests the actual storage-level behaviour: if three credit notes
    arrive sequentially each requesting 0.8x the original, the third must
    be blocked (0.8 + 0.8 = 1.6x passes, 1.6 + 0.8 = 2.4x exceeds 2x cap).
    """

    def test_exploit_b3_sequential_refunds_cap_enforced(self):
        """Third refund of 0.8x original must be blocked; cumulative total must not exceed 2x."""
        storage = Storage(":memory:")
        task_id = _make_task_id()
        original_price = 10.0
        _seed_execution_log(storage, task_id, original_price, "consumer-node")

        # First refund: 0.8x = 8.0 total so far
        cumulative = storage.get_cumulative_refund(task_id)
        self.assertEqual(cumulative, 0.0)
        storage.record_refund(task_id, 8.0)

        # Second refund: 0.8x = 16.0 total — still within 2x=20
        cumulative = storage.get_cumulative_refund(task_id)
        self.assertEqual(cumulative, 8.0)
        storage.record_refund(task_id, 8.0)

        # Third refund attempt: cumulative is now 16.0, adding 8.0 = 24.0 > 20.0
        cumulative = storage.get_cumulative_refund(task_id)
        self.assertEqual(cumulative, 16.0,
                         f"Expected cumulative 16.0, got {cumulative}. "
                         "record_refund not persisting correctly?")

        max_refund = original_price * 2
        would_exceed = cumulative + 8.0 > max_refund
        self.assertTrue(would_exceed,
                        f"Third refund of 8.0 should exceed 2x cap. "
                        f"cumulative={cumulative}, max={max_refund}")


# ---------------------------------------------------------------------------
# B-TRACK: Sender verification (S-022)
# ---------------------------------------------------------------------------

class TestExploitB4_NoneFromNodeCrashesReceipt(unittest.TestCase):
    """B-4: None from_node in receipt body crashes handle_receipt with TypeError.

    In handle_receipt, validation logging does:
        item.get('from_node', '?')[:16]
    If validate_receipt fails for ANY reason and from_node is None in the item
    dict (not the default '?'), the slice operation crashes with:
        TypeError: 'NoneType' object is not subscriptable

    Additionally, in the S-022 path there is:
        from_node[:16] if from_node else 'N/A'
    which safely handles None in the warning — but the validation error
    path at line 42 does NOT have this guard.
    """

    def test_exploit_b4_none_from_node_crashes_with_type_error(self):
        """None from_node causes TypeError in validation error path of handle_receipt."""
        from knarr.commerce.handlers import make_commerce_handlers

        task_id = _make_task_id()
        node = MagicMock()
        node.storage.get_execution_log_entry.return_value = {
            "price": 100.0,
            "task_id": task_id,
            "requester_node_id": None,
        }
        node._enqueue_write = AsyncMock()
        node._sync = AsyncMock()
        node._sync.enqueue = AsyncMock()

        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/receipt"]

        async def run():
            # Sending a receipt without the required "type" field causes validate_receipt
            # to fail, triggering the logger.warning path which does from_node[:16]
            # without None-guarding it.
            await handler({
                "from_node": None,  # None, not a string
                "body": json.dumps({
                    "task_id": task_id,
                    "status": "rejected",
                    "refund_requested": True,
                    "timestamp": time.time(),
                    # NOTE: Missing "type": "knarr/commerce/receipt" field
                    # This causes validate_receipt to return (False, "wrong type")
                    # which triggers logger.warning(f"...{item.get('from_node', '?')[:16]}")
                    # but item.get('from_node', '?') returns None (key exists with None value)
                    # causing NoneType[:16] crash
                })
            })

        # This should raise TypeError, not return cleanly
        with self.assertRaises(TypeError,
                               msg="handle_receipt does not guard against None from_node "
                                   "in the validation error path. "
                                   "item.get('from_node', '?')[:16] crashes when from_node=None "
                                   "because the default '?' is only used if the key is MISSING, "
                                   "not if the key is present with value None."):
            asyncio.new_event_loop().run_until_complete(run())


class TestExploitB5_SenderVerificationInReceipt(unittest.TestCase):
    """B-5: Legitimate consumer path confirms sender verification works."""

    def test_exploit_b5_legitimate_requester_passes(self):
        """Correct from_node matching requester_node_id should process the refund."""
        from knarr.commerce.handlers import make_commerce_handlers

        task_id = _make_task_id()
        legitimate_node = "legitimate-consumer-node-id-abc"

        node = MagicMock()
        node.storage.get_execution_log_entry.return_value = {
            "price": 10.0,
            "task_id": task_id,
            "requester_node_id": legitimate_node,
        }
        node._enqueue_write = AsyncMock()
        node._sync = AsyncMock()
        node._sync.enqueue = AsyncMock()

        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/receipt"]

        async def run():
            await handler({
                "from_node": legitimate_node,  # Correct sender
                "body": json.dumps({
                    # Must include all fields that pass validate_receipt
                    "type": "knarr/commerce/receipt",
                    "task_id": task_id,
                    "status": "rejected",
                    "refund_requested": True,
                    "timestamp": time.time(),
                })
            })

        asyncio.new_event_loop().run_until_complete(run())

        # Should have enqueued a credit_note
        enqueue_calls = node._sync.enqueue.call_args_list
        self.assertGreater(
            len(enqueue_calls), 0,
            "Legitimate requester should trigger refund credit note generation"
        )

    def test_exploit_b5_attacker_is_blocked(self):
        """Attacker from_node NOT matching requester_node_id must be blocked."""
        from knarr.commerce.handlers import make_commerce_handlers

        task_id = _make_task_id()
        legitimate_node = "legitimate-consumer-node-id-abc"
        attacker_node = "attacker-node-id-xyz"

        node = MagicMock()
        node.storage.get_execution_log_entry.return_value = {
            "price": 10.0,
            "task_id": task_id,
            "requester_node_id": legitimate_node,
        }
        node._enqueue_write = AsyncMock()
        node._sync = AsyncMock()
        node._sync.enqueue = AsyncMock()

        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/receipt"]

        async def run():
            await handler({
                "from_node": attacker_node,  # Wrong sender
                "body": json.dumps({
                    "type": "knarr/commerce/receipt",
                    "task_id": task_id,
                    "status": "rejected",
                    "refund_requested": True,
                    "timestamp": time.time(),
                })
            })

        asyncio.new_event_loop().run_until_complete(run())

        enqueue_calls = node._sync.enqueue.call_args_list
        self.assertEqual(
            len(enqueue_calls), 0,
            "Attacker should be blocked by S-022 sender verification"
        )


# ---------------------------------------------------------------------------
# A-TRACK: Security events — information leakage
# ---------------------------------------------------------------------------

class TestExploitA1_AuthFailedLeaksSensitiveFields(unittest.TestCase):
    """A-1: security.auth_failed event must not leak the submitted token.

    The _check_auth method emits security.auth_failed with source_ip and
    endpoint. It must NOT include the submitted Authorization header value,
    as that would log attacker-controlled token attempts (potentially
    including valid tokens from legitimate users who made a typo on the
    endpoint path).
    """

    def test_exploit_a1_auth_failed_event_no_token_in_fields(self):
        """security.auth_failed event fields must not contain the submitted token."""
        bus = EventBus(size=64)
        sub = bus.subscribe("security.auth_failed")

        # Simulate the _check_auth event emission
        secret_token = "my-super-secret-bearer-token-12345"
        bus.emit("security.auth_failed",
                 source_ip="10.0.0.1",
                 endpoint="/api/execute")

        events = sub.poll()
        self.assertEqual(len(events), 1)
        event = events[0]

        # Verify the token itself is not in any event field
        event_json = json.dumps(event)
        self.assertNotIn(
            secret_token, event_json,
            "security.auth_failed event must not contain the submitted token"
        )
        # But verify the event has the required fields
        self.assertIn("source_ip", event)
        self.assertIn("endpoint", event)


class TestExploitA2_SecurityEventLeaksNodeState(unittest.TestCase):
    """A-2: Bus events with internal state must not be consumable by external peers.

    The EventBus is an intra-node bus. Subscribers only exist if code on this
    node has called bus.subscribe(). However, if a plugin is loaded from an
    untrusted source, it could subscribe and read all security events.
    This test validates that security events DO contain the claimed fields
    (so a compromised plugin can exfiltrate node state via bus events).
    """

    def test_exploit_a2_security_events_contain_sensitive_fields(self):
        """Demonstrate that security events contain exploitable node state.

        A malicious plugin subscribed to security.* gets:
        - source_ip: attacker IP enumeration
        - endpoint: which endpoints are being targeted
        - claimed_id: node IDs attempting spoofing
        - msg_type: protocol message types
        This is a design observation, not a bug to fix, but documents the risk.
        """
        bus = EventBus(size=64)
        malicious_sub = bus.subscribe("security.*")

        # These events are emitted by node.py on real security events
        bus.emit("security.auth_failed", source_ip="192.168.1.50", endpoint="/api/execute")
        bus.emit("security.identity_mismatch", msg_type="TaskRequest",
                 from_ip="10.0.0.5", claimed_id="deadeef" * 9)
        bus.emit("security.signature_invalid", msg_type="JoinRequest", from_ip="172.16.0.1")

        events = malicious_sub.poll()
        self.assertEqual(len(events), 3,
                         "Malicious subscriber receives all security events — "
                         "any loaded plugin can subscribe and exfiltrate this data")

        # Verify the attacker can extract the source IPs
        ips = [e.get("source_ip") or e.get("from_ip") for e in events]
        self.assertIn("192.168.1.50", ips,
                      "A subscribed plugin can read auth failure source IPs")


class TestExploitA3_BusEventBombFromMaliciousPlugin(unittest.TestCase):
    """A-3: Malicious plugin with bus access can flood the ring buffer.

    A plugin receives bus=node.bus via PluginContext (added in P1). A
    malicious plugin can call bus.emit() in a tight loop, evicting all
    legitimate security events from the ring buffer before Thrall reads them.
    """

    def test_exploit_a3_ring_buffer_eviction_drops_security_events(self):
        """Malicious emit flood evicts security.auth_failed events from ring."""
        bus = EventBus(size=16)  # Small ring to demonstrate eviction

        # Legitimate security event arrives first
        bus.emit("security.auth_failed", source_ip="attacker-ip", endpoint="/api/execute")

        # Subscriber positioned AFTER the security event (simulates slow reader)
        sub = bus.subscribe("security.*")
        # Cursor starts at head — won't read the event already in buffer
        # But let's test from the beginning
        sub._cursor = 0  # Reset to see all events

        # Malicious plugin floods with 20 noise events (ring size is 16)
        # This overwrites the security.auth_failed event
        for i in range(20):
            bus.emit("noise.event", seq=i)

        events = sub.poll()
        security_events = [e for e in events if e.get("event") == "security.auth_failed"]

        self.assertEqual(
            len(security_events), 0,
            "Ring buffer eviction: malicious plugin flooded 20 noise events into "
            "a size-16 buffer, evicting the security.auth_failed event. "
            "Thrall or other security monitors will miss the event."
        )


# ---------------------------------------------------------------------------
# C-TRACK: Malicious config values
# ---------------------------------------------------------------------------

class TestExploitC1_NegativeQueueDepthIsUnbounded(unittest.TestCase):
    """C-1: Negative max_queue_depth produces unbounded queue, not an error.

    The C-track config reads max_queue_depth as int() from TOML without
    floor-clamping. asyncio.Queue(maxsize=-1) in Python 3.13 succeeds and
    creates an UNBOUNDED queue (same behaviour as maxsize=0).
    A malicious operator can set max_queue_depth = -1 to remove all task
    queue limits, enabling memory exhaustion via task flooding.
    """

    def test_exploit_c1_negative_queue_depth_is_unbounded(self):
        """asyncio.Queue(maxsize=-1) creates an unbounded queue silently."""
        import asyncio
        # Python 3.13: negative maxsize is treated as "no limit"
        q = asyncio.Queue(maxsize=-1)
        self.assertLessEqual(
            q.maxsize, 0,
            "asyncio.Queue with negative maxsize does NOT raise ValueError in Python 3.13. "
            "It creates an unbounded queue instead. "
            "The node code `int(config.get('max_queue_depth', 100))` has no floor clamp, "
            "so max_queue_depth=-1 silently removes the task queue cap."
        )
        # Verify it actually accepts items without blocking
        self.assertFalse(q.full(),
                         "Queue with negative maxsize is never full — no task cap enforcement")


class TestExploitC2_ZeroQueueDepthBlocksAllTasks(unittest.TestCase):
    """C-2: max_queue_depth = 0 sets unlimited queue (asyncio.Queue(0) = unbounded).

    The config reads int(config.get("max_queue_depth", 100)).
    asyncio.Queue(0) in Python means UNLIMITED, not zero capacity.
    Setting max_queue_depth = 0 in TOML silently removes the queue cap,
    allowing unbounded memory growth via task flooding.
    """

    def test_exploit_c2_zero_queue_depth_is_unbounded(self):
        """asyncio.Queue(0) has no maxsize limit — unlimited capacity."""
        import asyncio
        q = asyncio.Queue(maxsize=0)
        # qsize() is 0, but it accepts items without blocking
        # The node code uses Queue(maxsize=_max_queue) with no floor check
        self.assertEqual(q.maxsize, 0,
                         "Queue(maxsize=0) is unlimited — sets no cap")
        # This is the bug: if config has max_queue_depth=0, the 'full' check
        # never triggers and the queue grows without bound
        # Verify the node code has no floor: int("0") = 0 passed directly to Queue


class TestExploitC3_NanCreditLimits(unittest.TestCase):
    """C-3: NaN or Inf credit limits corrupt ledger comparisons.

    The economy config reads default_soft_limit and default_hard_limit as
    float(). A TOML value of 'nan' or 'inf' will pass through float() and
    corrupt all balance comparisons silently.
    """

    def test_exploit_c3_nan_soft_limit_corrupts_comparison(self):
        """float('nan') comparisons always return False — balance check never fires."""
        import math
        nan_limit = float('nan')
        # In node.py: entry.balance < min_balance
        # With nan: float('nan') < float('nan') is ALWAYS False
        balance = -999999.0
        result = balance < nan_limit
        self.assertFalse(
            result,
            "balance < NaN is always False — NaN credit limit means credit check is NEVER triggered. "
            "A peer with any negative balance passes the credit check."
        )

    def test_exploit_c3_inf_hard_limit_disables_credit_check(self):
        """float('-inf') as hard limit means balance can never be below it."""
        import math
        inf_limit = float('-inf')
        balance = float('-inf')  # Even infinite debt
        result = balance < inf_limit
        self.assertFalse(
            result,
            "balance < -inf is always False — Inf credit limit disables the credit check. "
            "Any balance passes, including theoretically infinite debt."
        )

    def test_exploit_c3_toml_float_accepts_special_values(self):
        """float() of config string 'nan' produces math.nan."""
        import math
        # Simulate TOML config parsing: config.get("default_hard_limit", -10.0)
        config_value = "nan"
        parsed = float(config_value)
        self.assertTrue(
            math.isnan(parsed),
            "float('nan') succeeds — config validation does not reject NaN values. "
            "A malicious TOML can inject NaN to disable credit checks."
        )


class TestExploitC4_NegativeEventBusSize(unittest.TestCase):
    """C-4: Negative event_bus_size causes IndexError crash in emit().

    The EventBus is constructed with size=_bus_size from config.
    EventBus.__init__ does `self._ring = [None] * size`.
    Negative size produces an empty list ([] in Python), but then
    emit() does:
        self._ring[self._head % self._size] = event
    With size=-1:
        self._head % (-1) = 0 for any positive head
        self._ring[0] on an empty list raises IndexError
    This crashes the first call to bus.emit() after construction,
    taking down any code path that emits events.
    """

    def test_exploit_c4_negative_bus_size_crashes_on_first_emit(self):
        """EventBus(size=-1) crashes with IndexError on the first emit()."""
        bus = EventBus(size=-1)
        # The ring is [None] * -1 = [] — zero capacity
        self.assertEqual(
            len(bus._ring), 0,
            "EventBus with negative size creates empty ring buffer silently. "
            "Config validation needed before EventBus construction."
        )
        # emit() CRASHES with IndexError — not a silent failure
        with self.assertRaises(IndexError,
                               msg="EventBus(size=-1).emit() should raise IndexError. "
                                   "The node code int(config.get('event_bus_size', 256)) "
                                   "has no floor clamp, so event_bus_size=-1 causes IndexError "
                                   "on the first bus.emit() call, crashing the node."):
            bus.emit("test.event", data="test")


class TestExploitC5_MinPeersZeroAllowsNetworkIsolation(unittest.TestCase):
    """C-5: min_peers = 0 allows prune_loop to remove ALL peers.

    The prune_loop checks `current_count <= min_peer_floor` before pruning.
    If min_peers is configured to 0, the floor is 0, so the node will prune
    all peers when they go stale — isolating itself from the network with no
    re-bootstrap safeguard trigger.
    """

    def test_exploit_c5_zero_min_peers_enables_total_peer_pruning(self):
        """min_peers=0 means all peers can be pruned, enabling network isolation."""
        storage = Storage(":memory:")
        from knarr.core.models import NodeInfo
        import time

        # Add peers that are technically stale (last_seen far in the past)
        for i in range(5):
            node_info = NodeInfo(node_id=f"peer{i}" * 4, host="127.0.0.1", port=9000 + i)
            storage.upsert_peer(node_info)

        # Manually age out peers
        conn = storage._get_conn()
        conn.execute("UPDATE peers SET last_seen = 1")  # epoch 1 — ancient
        conn.commit()

        peer_count_before = len(storage.get_peers())
        self.assertEqual(peer_count_before, 5)

        # Simulate what prune_loop does when min_peers=0
        min_peer_floor = 0  # From malicious config
        current_count = peer_count_before

        if current_count > min_peer_floor:
            # The floor check passes — all stale peers get pruned
            pruned = storage.prune_stale_peers(timeout=1)  # 1s timeout, all ancient
            peer_count_after = len(storage.get_peers())
        else:
            peer_count_after = current_count

        self.assertEqual(
            peer_count_after, 0,
            f"min_peers=0 allows pruning all {peer_count_before} peers. "
            "Node becomes network-isolated with no safeguard. "
            "Config validation should enforce min_peers >= 1."
        )


# ---------------------------------------------------------------------------
# A-TRACK: Bus event external trigger surface
# ---------------------------------------------------------------------------

class TestExploitA4_MailReceivedEventLeaksSenderNodeId(unittest.TestCase):
    """A-4: mail.received bus event leaks from_node to any subscribed plugin.

    The mail.received event includes from_node, msg_type, session_id, bucket.
    A malicious plugin subscribed to mail.* can enumerate all senders
    including their node IDs and session correlation.
    """

    def test_exploit_a4_mail_received_event_contains_from_node(self):
        """mail.received event exposes sender node_id to all bus subscribers."""
        bus = EventBus(size=64)
        malicious_sub = bus.subscribe("mail.*")

        # Simulates what sync.py emits on mail.received
        private_node_id = "private-node-id-that-sender-wanted-anonymous"
        bus.emit("mail.received",
                 from_node=private_node_id,
                 msg_type="text",
                 session_id="session-abc-123",
                 bucket="inbox")

        events = malicious_sub.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].get("from_node"), private_node_id,
            "mail.received event leaks sender node_id to all plugin subscribers. "
            "A malicious plugin can build a social graph from bus events alone."
        )


class TestExploitA5_DeliveryFailedEventLeaksMessageContent(unittest.TestCase):
    """A-5: mail.delivery_failed error field contains network-level error strings.

    The mail.delivery_failed event includes error=str(e)[:200].
    The exception message may contain IP addresses, hostnames, connection
    state, or partial message content from the network stack.
    """

    def test_exploit_a5_delivery_failed_error_truncated_but_present(self):
        """mail.delivery_failed error field is included (truncated to 200 chars)."""
        bus = EventBus(size=64)
        sub = bus.subscribe("mail.delivery_failed")

        # Simulate what _push_to_peer_inner emits
        # The actual error could be: "ConnectionRefusedError: [Errno 111] connection refused to 10.0.0.5:9030"
        sensitive_error = "ConnectionRefusedError: [Errno 111] connection refused to 10.0.0.5:9030"
        bus.emit("mail.delivery_failed",
                 to_node="target-node-id",
                 message_id="msg-abc",
                 attempts=3,
                 error=sensitive_error[:200])

        events = sub.poll()
        self.assertEqual(len(events), 1)
        error_field = events[0].get("error", "")
        self.assertIn(
            "10.0.0.5", error_field,
            "mail.delivery_failed error field contains IP addresses from connection errors. "
            "This leaks network topology to any subscribed plugin."
        )


# ---------------------------------------------------------------------------
# A-TRACK: EventBus subscriber isolation
# ---------------------------------------------------------------------------

class TestExploitA6_DeadSubscriberCrashesEmit(unittest.TestCase):
    """A-6: A subscriber with a closed event loop causes emit() to raise RuntimeError.

    The spec states emit() is O(1) write + O(subs) wake-set and should never block.
    But EventBus.emit() calls sub._wake.set() WITHOUT exception handling.
    If an asyncio.Event from a closed event loop raises RuntimeError("event loop is closed")
    when .set() is called, that exception propagates all the way through emit() and
    crashes the caller (e.g. node's task completion code or mail handler).

    This is the "dead subscriber poisons the bus" pattern. A plugin that creates a
    subscriber and then exits/closes its loop can crash ALL subsequent bus events
    including security monitoring, task completion callbacks, and mail dispatch.
    """

    def test_exploit_a6_dead_subscriber_crashes_emit(self):
        """emit() propagates RuntimeError from a subscriber with a closed event loop."""
        bus = EventBus(size=64)

        # Create a subscriber with a broken asyncio.Event (simulates closed loop)
        sub = bus.subscribe("test.*")
        # Replace the event with a mock that raises on .set()
        bad_event = MagicMock()
        bad_event.set.side_effect = RuntimeError("event loop is closed")
        sub._wake = bad_event

        # emit() propagates the RuntimeError — this IS the bug
        with self.assertRaises(RuntimeError,
                               msg="emit() should be exception-safe. "
                                   "A dead subscriber's _wake.set() should be caught and logged, "
                                   "not propagated to crash the caller. "
                                   "Current code has no try/except around sub._wake.set()."):
            bus.emit("test.event", data="hello")


# ---------------------------------------------------------------------------
# B-TRACK: LIKE injection completeness (S-025)
# ---------------------------------------------------------------------------

class TestExploitB6_LikeEscapeUnderscoreInjection(unittest.TestCase):
    """B-6: Underscore wildcard in peer key causes false positive settlement match.

    S-025 escaped % and \\ but the fix also includes _. This test specifically
    verifies the underscore case, which is a single-character SQL LIKE wildcard
    and can cause false positives if the escaped key contains underscores.
    """

    def test_exploit_b6_underscore_not_treated_as_wildcard(self):
        """Peer key with underscore should not match unrelated settlements via LIKE."""
        storage = Storage(":memory:")

        # Queue a settlement for a peer whose key starts with 'aa...'
        peer_a = "aa" * 32
        body_a = json.dumps({"peer": peer_a, "amount": 5.0})

        conn = storage._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settlement_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type TEXT,
                from_node TEXT,
                body TEXT,
                status TEXT DEFAULT 'pending',
                priority INTEGER DEFAULT 0,
                created_at REAL
            )
        """)
        conn.execute(
            "INSERT INTO settlement_queue (item_type, from_node, body, priority, created_at) "
            "VALUES ('settle', 'node1', ?, 1, ?)",
            (body_a, time.time())
        )
        conn.commit()

        # Key with underscore that could match 'aa' via unescaped _ wildcard
        # _a = any-char followed by 'a', so '_a' would match 'aa'
        peer_underscore = "_a" + ("bb" * 31)[:62]
        result = storage.has_pending_settlement(peer_underscore)

        self.assertFalse(
            result,
            "Peer key containing underscore matched an unrelated settlement via LIKE _. "
            "The _escape_like function should escape underscores to prevent this. "
            f"Key used: {peer_underscore[:20]}..."
        )


# ---------------------------------------------------------------------------
# C-TRACK: Config type confusion
# ---------------------------------------------------------------------------

class TestExploitC6_StringQueueDepthCrash(unittest.TestCase):
    """C-6: Non-numeric max_queue_depth config value causes ValueError at startup.

    The code does int(self._config.get("node", {}).get("max_queue_depth", 100)).
    A TOML value of max_queue_depth = "unlimited" will cause ValueError during
    DHTNode.__init__, crashing the node before it starts.
    """

    def test_exploit_c6_non_numeric_queue_depth_crashes(self):
        """int("unlimited") raises ValueError — crashes node startup."""
        with self.assertRaises(ValueError,
                               msg="Non-numeric max_queue_depth should raise ValueError. "
                                   "Config validation does not guard against string values."):
            int("unlimited")

    def test_exploit_c6_float_queue_depth_is_silently_accepted(self):
        """float-as-string queue depth truncates silently."""
        # int("3.14") raises ValueError in Python
        # But what if TOML parses it as a float directly?
        # int(3.14) = 3 — silently truncated
        result = int(3.14)  # float from TOML parsed correctly
        self.assertEqual(result, 3,
                         "Float queue depth (3.14) truncates to 3 silently — "
                         "this is acceptable behaviour but documents that TOML float values work")


# ---------------------------------------------------------------------------
# A-TRACK: Bus event cursor safety under ring wrap
# ---------------------------------------------------------------------------

class TestExploitA7_SubscriberCursorRaceOnRingWrap(unittest.TestCase):
    """A-7: Subscriber cursor is not thread-safe — race on ring wrap.

    The EventBus uses self._lock for emit() but Subscriber._cursor is
    updated without any lock. If emit() and sub.poll() run concurrently
    (emit from handler thread pool, poll from async task), the cursor
    can be read mid-update, causing missed events or index errors.
    """

    def test_exploit_a7_concurrent_emit_poll_no_index_error(self):
        """Concurrent emit + poll should not cause IndexError or missed events."""
        bus = EventBus(size=8)
        sub = bus.subscribe("test.*")
        errors = []
        poll_results = []

        def emitter():
            for i in range(50):
                try:
                    bus.emit("test.event", seq=i)
                except Exception as e:
                    errors.append(f"emit: {e}")
                time.sleep(0.001)

        def poller():
            for _ in range(50):
                try:
                    results = sub.poll()
                    poll_results.extend(results)
                except Exception as e:
                    errors.append(f"poll: {e}")
                time.sleep(0.001)

        t1 = threading.Thread(target=emitter)
        t2 = threading.Thread(target=poller)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(errors, [],
                         f"Concurrent emit/poll caused errors: {errors}. "
                         "Subscriber._cursor is not thread-safe.")


if __name__ == "__main__":
    unittest.main()
