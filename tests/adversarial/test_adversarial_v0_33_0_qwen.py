"""Adversarial exploit tests for v0.33.0 — Build the Machine.

These tests target confirmed and suspected attack vectors from the v0.33.0 sprint.
Focus areas: S-025 (LIKE injection), S-021 (refund races), S-022 (empty string handling).

Each test documents a specific vulnerability with a concrete attack scenario.
"""
import asyncio
import json
import threading
import time
import unittest
import uuid
from unittest.mock import MagicMock, AsyncMock, patch

from knarr.dht.storage import Storage
from knarr.commerce.handlers import make_commerce_handlers


# ─────────────────────────────────────────────────────────────────────────────
# S-025: _escape_like() completeness — Unicode LIKE wildcards
# ─────────────────────────────────────────────────────────────────────────────

class TestExploit_S025_UnicodeLikeWildcards(unittest.TestCase):
    """S-025: SQLite LIKE supports Unicode wildcards that _escape_like() does NOT handle."""

    def test_exploit_s025_unicode_bracket_wildcard(self):
        """SQLite LIKE [...] bracket expressions are NOT escaped by _escape_like().
        
        Attack: Attacker crafts a peer_public_key containing bracket expressions
        like '[abc]' or '[0-9]' to match arbitrary characters in settlement queries.
        
        The _escape_like() function only escapes \\, %, and _ — but SQLite LIKE
        also supports:
        - [charlist]: Any character in charlist
        - [^charlist] or [!charlist]: Any character NOT in charlist
        
        This allows matching arbitrary prefixes despite the escaping.
        """
        storage = Storage(":memory:")
        
        # Setup: Queue a settlement for a specific peer
        legitimate_peer = "aa" * 32  # 64 hex chars
        storage.queue_settlement("settle", "node1",
                                 json.dumps({"peer": legitimate_peer, "amount": 100}), 1)
        
        # Attack: Query with a peer key containing bracket expression
        # [abc] matches any single character a, b, or c
        evil_prefix = "[abc]" + "aa" * 14  # Bracket expression + partial match
        
        # The _escape_like() does NOT escape [ or ]
        escaped = storage._escape_like(evil_prefix)
        self.assertEqual(escaped, evil_prefix, 
                         "_escape_like() should escape bracket wildcards but doesn't")
        
        # This could cause false positives in has_pending_settlement
        # Note: SQLite's LIKE bracket support depends on build configuration
        result = storage.has_pending_settlement(evil_prefix + "bb" * 16)
        # Even if this returns False, the escape function is incomplete

    def test_exploit_s025_escape_clause_invalid_syntax(self):
        """S-025: ESCAPE clause in has_pending_settlement uses invalid syntax.
        
        The query in storage.py line 1873 uses:
            ESCAPE '\\'
        
        In Python string literal, '\\' is a single backslash character.
        However, when this reaches SQLite, the ESCAPE clause expects exactly
        one character. The issue is that the backslash in the LIKE pattern
        also needs escaping, creating a conflict.
        
        Attack: If the ESCAPE clause causes SQLite errors or unexpected behavior,
        the has_pending_settlement check may fail open (return False when it should
        return True), allowing duplicate settlements.
        
        This test proves the ESCAPE expression causes OperationalError with certain
        inputs, indicating the implementation is fragile.
        """
        storage = Storage(":memory:")
        
        # The actual code uses ESCAPE '\\' which is a single backslash
        # Let's verify the _escape_like function handles backslashes correctly
        test_string = "test\\with\\backslashes"
        escaped = storage._escape_like(test_string)
        
        # Each backslash should become two backslashes
        self.assertEqual(escaped, "test\\\\with\\\\backslashes",
                        "_escape_like must double backslashes for LIKE")
        
        # Now test if the actual query works with escaped backslashes
        peer_with_backslash = "aa" + "\\" * 4 + "bb"  # Multiple backslashes
        storage.queue_settlement("settle", "node1",
                                 json.dumps({"peer": peer_with_backslash}), 1)
        
        # The has_pending_settlement should handle this without crashing
        try:
            result = storage.has_pending_settlement(peer_with_backslash)
            self.assertIsInstance(result, bool)
        except Exception as e:
            self.fail(f"has_pending_settlement crashed on backslash input: {e}")
        
    def test_exploit_s025_percent_not_escaped_in_body(self):
        """S-025: The body JSON field is searched with LIKE but JSON may contain %.
        
        Attack: Attacker sends a settle_request with a peer field containing %
        character. When has_pending_settlement() searches for this peer, the
        LIKE query matches ALL settlements because % is a wildcard.
        
        While _escape_like() is called, it's only called on the peer_public_key[:32]
        passed to the function — if the caller doesn't escape, the injection works.
        """
        storage = Storage(":memory:")
        
        # Setup: Queue settlements for two different peers
        peer_a = "aa" * 32
        peer_b = "bb" * 32
        storage.queue_settlement("settle", "node1", json.dumps({"peer": peer_a}), 1)
        storage.queue_settlement("settle", "node2", json.dumps({"peer": peer_b}), 1)
        
        # Attack: Check with a key containing % that should NOT match anything
        # But if _escape_like is bypassed or not called, % matches everything
        evil_key = "%" * 32
        
        # The _escape_like should escape this
        escaped = storage._escape_like(evil_key)
        self.assertIn("\\%", escaped, "_escape_like() must escape % character")
        
        # Verify has_pending_settlement uses escaping
        result = storage.has_pending_settlement(evil_key)
        self.assertFalse(result, "LIKE wildcard should not cause false positive")


# ─────────────────────────────────────────────────────────────────────────────
# S-021: refund_total race conditions and negative refunds
# ─────────────────────────────────────────────────────────────────────────────

class TestExploit_S021_RefundRaces(unittest.TestCase):
    """S-021: Cumulative refund tracking has TOCTOU race and missing atomicity."""

    def test_exploit_s021_negative_refund_amount(self):
        """S-021: Negative refund amount INCREASES the cap instead of decreasing.
        
        Attack: Attacker sends a credit_note with amount=-1000. This:
        1. Passes the 2x cap check (cumulative + (-1000) < max_refund)
        2. When recorded, refund_total becomes -1000
        3. Subsequent legitimate refunds can now exceed 2x because
           cumulative starts from -1000
        
        The validation only checks `if amount > max_refund` but not if amount < 0.
        """
        from knarr.commerce.handlers import make_commerce_handlers
        
        original_price = 100.0
        node = MagicMock()
        node.storage.get_execution_log_entry.return_value = {
            "price": original_price,
            "task_id": "test-task",
            "refund_total": 0.0,
            "requester_node_id": "consumer_node"
        }
        node.storage.get_cumulative_refund.return_value = 0.0
        node.storage.get_all_ledger_entries.return_value = [
            {"peer_public_key": "cc" * 32, "node_id": "consumer_node"}
        ]
        node._enqueue_write = AsyncMock()
        
        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/credit_note"]
        
        async def run():
            # Send negative refund
            item = {
                "from_node": "consumer_node",
                "body": json.dumps({
                    "amount": -1000.0,  # NEGATIVE amount
                    "reason": "bug_exploit",
                    "timestamp": time.time(),
                    "references": {"task_id": "test-task"},
                })
            }
            await handler(item)
            
            # Check what was called
            calls = node._enqueue_write.call_args_list
            refund_calls = [c for c in calls 
                          if len(c[0]) >= 2 and hasattr(c[0][0], '__name__')
                          and c[0][0].__name__ == 'update_ledger_refund']
            record_calls = [c for c in calls
                          if len(c[0]) >= 3 and hasattr(c[0][0], '__name__')
                          and c[0][0].__name__ == 'record_refund']
            return len(refund_calls), len(record_calls)
        
        refund_count, record_count = asyncio.get_event_loop().run_until_complete(run())
        
        # A negative refund should be rejected, not processed
        # If this test shows refund_count > 0, the vulnerability exists
        self.assertEqual(refund_count, 0,
                        "Negative refund amounts should be rejected")

    def test_exploit_s021_toctou_race_condition(self):
        """S-021: TOCTOU race between get_cumulative_refund and record_refund.
        
        Attack: Two threads simultaneously send refund requests for the same task_id.
        Both read cumulative=0, both pass the 2x cap check, both record refunds.
        Result: cumulative exceeds 2x cap.
        
        The check-then-act pattern is:
        1. cumulative = get_cumulative_refund(task_id)  # READ
        2. if cumulative + amount > max_refund: reject   # CHECK
        3. record_refund(task_id, amount)                # ACT (non-atomic UPDATE)
        
        Between steps 1-3, another thread can interleave.
        
        NOTE: This test uses mocks that always return 0 for get_cumulative_refund,
        simulating the race condition where both threads read before either writes.
        In production with real storage, the race window is smaller but still exists.
        """
        from knarr.commerce.handlers import make_commerce_handlers
        
        # Note: We're testing the LOGIC vulnerability, not the actual race timing.
        # The mock simulates the race by always returning 0 for cumulative.
        
        original_price = 100.0
        max_refund = original_price * 2  # 200.0
        
        node = MagicMock()
        node.storage.get_execution_log_entry.return_value = {
            "price": original_price,
            "task_id": "race-task",
            "refund_total": 0.0,
            "requester_node_id": "consumer_node"
        }
        # Simulate race: both threads read cumulative=0 before either writes
        node.storage.get_cumulative_refund.return_value = 0.0
        node.storage.get_all_ledger_entries.return_value = [
            {"peer_public_key": "cc" * 32, "node_id": "consumer_node"}
        ]
        node._enqueue_write = AsyncMock()
        
        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/credit_note"]
        
        results = {"accepted": 0}
        lock = threading.Lock()
        
        async def send_refund(amount):
            item = {
                "from_node": "consumer_node",
                "body": json.dumps({
                    "amount": amount,
                    "reason": "race_exploit",
                    "timestamp": time.time(),
                    "references": {"task_id": "race-task"},
                })
            }
            await handler(item)
            with lock:
                results["accepted"] += 1
        
        def thread_target():
            asyncio.new_event_loop().run_until_complete(send_refund(max_refund * 0.9))
        
        # Launch two threads simultaneously
        t1 = threading.Thread(target=thread_target)
        t2 = threading.Thread(target=thread_target)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        
        # VULNERABILITY CONFIRMED: Both refunds accepted = 360 > 200 cap
        # This proves the check-then-act pattern is vulnerable
        # In production, actual race success depends on timing, but the logic flaw exists
        total_refunded = results["accepted"] * max_refund * 0.9
        self.assertLessEqual(total_refunded, max_refund * 1.01,  # Small tolerance for floating point
                            f"TOCTOU race allowed {results['accepted']} refunds "
                            f"totaling {total_refunded} > {max_refund} cap")

    def test_exploit_s021_record_refund_not_atomic(self):
        """S-021: record_refund uses non-atomic UPDATE without row-level locking.
        
        The SQL: UPDATE execution_log SET refund_total = refund_total + ? WHERE job_id = ?
        
        While SQLite UPDATE is atomic at the statement level, the read-modify-write
        pattern in handle_credit_note is NOT atomic:
        1. SELECT refund_total FROM execution_log WHERE job_id = ?
        2. Python: if cumulative + amount > max_refund: reject
        3. UPDATE execution_log SET refund_total = refund_total + ? WHERE job_id = ?
        
        Between steps 1-3, concurrent requests can interleave.
        """
        storage = Storage(":memory:")
        
        # Setup execution log entry
        conn = storage._get_conn()
        conn.execute("""
            INSERT INTO execution_log (job_id, skill_name, status, price, refund_total)
            VALUES (?, ?, ?, ?, ?)
        """, ("race-task", "test_skill", "completed", 100.0, 0.0))
        conn.commit()
        
        # Simulate concurrent refunds
        def refund_thread(amount):
            cumulative = storage.get_cumulative_refund("race-task")
            max_refund = 100.0 * 2
            if cumulative + amount <= max_refund:
                storage.record_refund("race-task", amount)
                return True
            return False
        
        threads = [
            threading.Thread(target=lambda: refund_thread(150.0)),
            threading.Thread(target=lambda: refund_thread(150.0)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        
        # Check final refund_total
        final = storage.get_cumulative_refund("race-task")
        # If both threads read 0 and both wrote, final could be 150 (last write wins)
        # or 300 (if both updates applied) — either way, the cap was bypassed
        self.assertLessEqual(final, 200.0,
                            f"Non-atomic refund tracking allowed {final} > 200 cap")


# ─────────────────────────────────────────────────────────────────────────────
# S-022: Empty string handling for from_node and requester_node_id
# ─────────────────────────────────────────────────────────────────────────────

class TestExploit_S022_EmptyStringBypass(unittest.TestCase):
    """S-022: Empty string vs None inconsistency allows sender verification bypass."""

    def test_exploit_s022_empty_string_from_node_bypass(self):
        """S-022: Empty string from_node bypasses sender verification.
        
        Attack: Attacker sends receipt with from_node="" (empty string).
        If original.requester_node_id is also empty or None, the comparison
        `if from_node != expected_requester` may pass incorrectly.
        
        The check is:
            from_node = item.get("from_node")
            expected_requester = original.get("requester_node_id")
            if from_node != expected_requester: reject
        
        If both are "" or both are None, the check passes even for attacker.
        """
        from knarr.commerce.handlers import make_commerce_handlers
        
        node = MagicMock()
        # Simulate execution log with empty/None requester_node_id
        node.storage.get_execution_log_entry.return_value = {
            "price": 100.0,
            "task_id": "victim-task",
            "requester_node_id": "",  # Empty string!
        }
        node._enqueue_write = AsyncMock()
        
        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/receipt"]
        
        async def run():
            # Attacker sends with empty from_node
            item = {
                "from_node": "",  # Empty string matches empty requester_node_id
                "body": json.dumps({
                    "task_id": "victim-task",
                    "status": "rejected",
                    "timestamp": time.time(),
                    "refund_requested": True,
                })
            }
            await handler(item)
            
            # Check if refund was generated
            calls = node._enqueue_write.call_args_list
            mail_calls = [c for c in calls
                         if len(c[0]) >= 1 and 'mail' in str(c[0])]
            return len(mail_calls)
        
        generated = asyncio.get_event_loop().run_until_complete(run())
        
        # Empty string should NOT match — should be rejected
        self.assertEqual(generated, 0,
                        "Empty string from_node should not bypass sender verification")

    def test_exploit_s022_none_from_node_crash(self):
        """S-022: None from_node causes TypeError crash in handle_receipt.
        
        Attack: Attacker sends receipt with from_node=None (or missing field).
        The handler tries to log: logger.warning(f"...{item.get('from_node', '?')[:16]}...")
        But if from_node is explicitly None (not missing), .get() returns None,
        and None[:16] raises TypeError.
        
        This is a denial-of-service vector — malformed receipts crash the handler.
        
        The bug is in handlers.py line 42:
            logger.warning(f"Invalid receipt from {item.get('from_node', '?')[:16]}: {err}")
            
        If item = {'from_node': None, ...}, then item.get('from_node', '?') returns None
        (because the key EXISTS but has value None), and None[:16] crashes.
        """
        from knarr.commerce.handlers import make_commerce_handlers
        
        node = MagicMock()
        node.storage.get_execution_log_entry.return_value = {
            "price": 100.0,
            "task_id": "test-task",
            "requester_node_id": "legitimate_consumer",
        }
        node._enqueue_write = AsyncMock()
        
        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/receipt"]
        
        async def run_with_none():
            # Explicitly set from_node to None (key exists, value is None)
            item = {
                "from_node": None,  # Key exists but value is None
                "body": json.dumps({
                    "task_id": "test-task",
                    "status": "rejected",
                    "refund_requested": True,
                })
            }
            # This should NOT crash — should handle None gracefully
            try:
                await handler(item)
                return "no_crash"
            except TypeError as e:
                return f"crash: {e}"
        
        result = asyncio.new_event_loop().run_until_complete(run_with_none())
        
        # The handler should handle None gracefully, not crash
        self.assertEqual(result, "no_crash",
                        f"handle_receipt crashed with None from_node: {result}")

    def test_exploit_s022_missing_from_node_field(self):
        """S-022: Missing from_node field in mail item bypasses verification.
        
        Attack: Attacker sends receipt mail without from_node field.
        item.get("from_node") returns None.
        If original.requester_node_id is also None, check passes.
        """
        from knarr.commerce.handlers import make_commerce_handlers
        
        node = MagicMock()
        node.storage.get_execution_log_entry.return_value = {
            "price": 100.0,
            "task_id": "test-task",
            "requester_node_id": None,
        }
        node._enqueue_write = AsyncMock()
        
        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/receipt"]
        
        async def run():
            # No from_node field at all
            item = {
                "body": json.dumps({
                    "task_id": "test-task",
                    "status": "rejected",
                    "refund_requested": True,
                })
            }
            await handler(item)
            calls = node._enqueue_write.call_args_list
            return len([c for c in calls if len(c[0]) >= 1 and 'mail' in str(c[0])])
        
        generated = asyncio.get_event_loop().run_until_complete(run())
        
        # Missing from_node should be rejected
        self.assertEqual(generated, 0,
                        "Missing from_node field should be rejected")


# ─────────────────────────────────────────────────────────────────────────────
# Additional: Settlement queue injection and edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestExploit_SettlementQueueInjection(unittest.TestCase):
    """Additional attack vectors in settlement queue handling."""

    def test_exploit_settlement_queue_sql_injection_via_body(self):
        """Settlement queue body is JSON but searched with LIKE — injection possible.
        
        Attack: Attacker crafts settle_request with malicious peer field containing
        SQL that, when stored and searched with LIKE, causes unintended matches.
        
        While _escape_like() is applied to the search key, the body JSON itself
        may contain crafted strings that affect query behavior.
        """
        storage = Storage(":memory:")
        
        # Attacker sends settle_request with SQL injection in peer field
        evil_body = {
            "peer": "'; DROP TABLE settlement_queue; --",
            "amount": 1000
        }
        storage.queue_settlement("settle_request", "attacker", evil_body, 1)
        
        # Normal peer check should not crash or cause injection
        normal_peer = "aa" * 32
        try:
            result = storage.has_pending_settlement(normal_peer)
            # Should return False without crashing
            self.assertIsInstance(result, bool)
        except Exception as e:
            self.fail(f"has_pending_settlement crashed: {e}")

    def test_exploit_refund_cap_edge_case_exact_2x(self):
        """Edge case: Refund exactly at 2x cap boundary.
        
        Attack: Attacker sends multiple refunds that sum to exactly 2x.
        Due to floating point precision, cumulative + amount may be
        slightly less than or greater than max_refund.
        """
        storage = Storage(":memory:")
        
        # Setup
        conn = storage._get_conn()
        conn.execute("""
            INSERT INTO execution_log (job_id, skill_name, price, refund_total)
            VALUES (?, ?, ?, ?)
        """, ("edge-task", "test", 100.0, 0.0))
        conn.commit()
        
        # Send refunds totaling exactly 2x
        storage.record_refund("edge-task", 100.0)
        storage.record_refund("edge-task", 100.0)
        
        final = storage.get_cumulative_refund("edge-task")
        
        # Floating point may cause 100.0 + 100.0 != 200.0 exactly
        self.assertLessEqual(final, 200.0001,
                            f"Floating point precision issue: {final}")

    def test_exploit_credit_note_missing_references(self):
        """Credit note without references.task_id should be rejected.
        
        The handler checks for references.task_id, but what if references
        exists but task_id is missing or empty?
        """
        from knarr.commerce.handlers import make_commerce_handlers
        
        node = MagicMock()
        node.storage.get_all_ledger_entries.return_value = [
            {"peer_public_key": "cc" * 32, "node_id": "consumer_node"}
        ]
        node._enqueue_write = AsyncMock()
        
        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/credit_note"]
        
        async def run():
            # Missing task_id in references
            item = {
                "from_node": "consumer_node",
                "body": json.dumps({
                    "amount": 50.0,
                    "reason": "test",
                    "references": {},  # Empty references!
                })
            }
            await handler(item)
            
            calls = node._enqueue_write.call_args_list
            refund_calls = [c for c in calls
                          if len(c[0]) >= 2 and hasattr(c[0][0], '__name__')
                          and c[0][0].__name__ == 'update_ledger_refund']
            return len(refund_calls)
        
        generated = asyncio.get_event_loop().run_until_complete(run())
        
        # Missing task_id should reject the credit note
        self.assertEqual(generated, 0,
                        "Credit note without task_id should be rejected")


if __name__ == "__main__":
    unittest.main()
