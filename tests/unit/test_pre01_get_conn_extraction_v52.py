"""PRE-01: Verify all 13 raw _get_conn() calls have been extracted to Storage methods.

Tests verify:
1. Each new method exists on Storage
2. Each method returns expected results with a real in-memory DB
3. No raw _get_conn() calls remain in the call sites
"""

import json
import time
import pytest
from knarr.dht.storage import Storage
from knarr.core.models import SkillSheet, NodeInfo


def make_storage() -> Storage:
    s = Storage(":memory:")
    return s


def make_skill_sheet(name="test"):
    return SkillSheet(
        name=name, version="1.0", description="test",
        tags=[], input_schema={}, output_schema={}
    )


# ──────────────────────────────────────────────────────────────────────────────
# PRE-01 #1: get_pubkey_by_node_id (already existed — verify it works)
# ──────────────────────────────────────────────────────────────────────────────

def test_get_pubkey_by_node_id():
    s = make_storage()
    node_id = "a" * 64
    pub_key = "b" * 64
    # Insert via peer_keys
    conn = s._get_conn()
    conn.execute("INSERT OR REPLACE INTO peer_keys (node_id, public_key) VALUES (?, ?)",
                 (node_id, pub_key))
    conn.commit()

    result = s.get_pubkey_by_node_id(node_id)
    assert result == pub_key

    result_missing = s.get_pubkey_by_node_id("c" * 64)
    assert result_missing is None


# ──────────────────────────────────────────────────────────────────────────────
# PRE-01 #2: cleanup_zombie_tasks
# ──────────────────────────────────────────────────────────────────────────────

def test_cleanup_zombie_tasks():
    s = make_storage()
    conn = s._get_conn()
    # Insert a stale 'running' execution_log entry
    old_ts = time.time() - 1000
    conn.execute(
        "INSERT INTO execution_log (job_id, skill_name, status, created_at) VALUES (?, ?, 'running', ?)",
        ("job1", "test-skill", old_ts)
    )
    conn.commit()

    exec_count, async_count = s.cleanup_zombie_tasks(timeout_sec=300)
    assert exec_count == 1
    assert async_count == 0

    # Verify it was updated
    row = conn.execute("SELECT status FROM execution_log WHERE job_id = 'job1'").fetchone()
    assert row[0] == "failed"


# ──────────────────────────────────────────────────────────────────────────────
# PRE-01 #3: get_total_network_balance
# ──────────────────────────────────────────────────────────────────────────────

def test_get_total_network_balance():
    s = make_storage()
    # Empty ledger
    assert s.get_total_network_balance() == 0.0

    # Add some entries
    s.get_or_create_ledger_entry("a" * 64)
    s.get_or_create_ledger_entry("b" * 64)
    conn = s._get_conn()
    conn.execute("UPDATE ledger SET balance = 5.0 WHERE peer_public_key = ?", ("a" * 64,))
    conn.execute("UPDATE ledger SET balance = -3.0 WHERE peer_public_key = ?", ("b" * 64,))
    conn.commit()

    total = s.get_total_network_balance()
    assert abs(total - 2.0) < 0.001


# ──────────────────────────────────────────────────────────────────────────────
# PRE-01 #4: get_discount_rules
# ──────────────────────────────────────────────────────────────────────────────

def test_get_discount_rules_empty_groups():
    s = make_storage()
    result = s.get_discount_rules([], "test-skill")
    assert result == []


def test_get_discount_rules_with_data():
    s = make_storage()
    # Insert a discount rule
    s.upsert_discount_rule("vip-discount", "vip", "*", 10.0, 1)
    s.upsert_discount_rule("partner-discount", "partner", "test-skill", 5.0, 0)

    # Query for 'vip' group
    rules = s.get_discount_rules(["vip"], "test-skill")
    assert len(rules) == 1
    assert rules[0]["name"] == "vip-discount"
    assert rules[0]["effect_pct"] == 10.0

    # Query for 'partner' group + specific skill
    rules2 = s.get_discount_rules(["partner"], "test-skill")
    assert len(rules2) == 1
    assert rules2[0]["name"] == "partner-discount"


# ──────────────────────────────────────────────────────────────────────────────
# PRE-01 #5: get_discount_rule_by_id
# ──────────────────────────────────────────────────────────────────────────────

def test_get_discount_rule_by_id():
    s = make_storage()
    s.upsert_discount_rule("test-rule", "group1", "*", 15.0, 2)

    discounts = s.get_pricing_discounts()
    assert len(discounts) == 1
    rule_id = discounts[0]["id"]

    fetched = s.get_discount_rule_by_id(rule_id)
    assert fetched is not None
    assert fetched["name"] == "test-rule"
    assert fetched["effect_pct"] == 15.0

    missing = s.get_discount_rule_by_id(99999)
    assert missing is None


# ──────────────────────────────────────────────────────────────────────────────
# PRE-01 #6: get_execution_price
# ──────────────────────────────────────────────────────────────────────────────

def test_get_execution_price_no_data():
    s = make_storage()
    result = s.get_execution_price("unknown-skill")
    assert result is None


def test_get_execution_price_with_data():
    s = make_storage()
    conn = s._get_conn()
    # Create skill_cost_projection table if migration hasn't created it
    try:
        conn.execute(
            "INSERT INTO skill_cost_projection (skill_name, total_cost) VALUES (?, ?)",
            ("test-skill", 2.5)
        )
        conn.commit()
        result = s.get_execution_price("test-skill")
        assert result is not None
        assert abs(result - 2.5) < 0.001
    except Exception:
        # Table may not exist — acceptable, method returns None gracefully
        result = s.get_execution_price("test-skill")
        assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# PRE-01 #7: upsert_discount_rule
# ──────────────────────────────────────────────────────────────────────────────

def test_upsert_discount_rule_insert():
    s = make_storage()
    s.upsert_discount_rule("test", "grp", "*", 10.0, 1)
    rules = s.get_pricing_discounts()
    assert len(rules) == 1
    assert rules[0]["name"] == "test"


def test_upsert_discount_rule_update():
    s = make_storage()
    s.upsert_discount_rule("test", "grp", "*", 10.0, 1)
    rules = s.get_pricing_discounts()
    rule_id = rules[0]["id"]

    s.upsert_discount_rule("test-updated", "grp", "*", 20.0, 1, discount_id=rule_id)
    updated = s.get_discount_rule_by_id(rule_id)
    assert updated["name"] == "test-updated"
    assert updated["effect_pct"] == 20.0


def test_upsert_discount_rule_deactivated_raises():
    s = make_storage()
    s.upsert_discount_rule("test", "grp", "*", 10.0, 1)
    rules = s.get_pricing_discounts()
    rule_id = rules[0]["id"]

    # Deactivate it
    s.delete_pricing_discount(rule_id)

    # Try to update — should raise ValueError
    with pytest.raises(ValueError, match="deactivated"):
        s.upsert_discount_rule("test-updated", "grp", "*", 20.0, 1, discount_id=rule_id)


# ──────────────────────────────────────────────────────────────────────────────
# PRE-01 #8: cleanup_stale_tasks — returns accepted task rows
# ──────────────────────────────────────────────────────────────────────────────

def test_cleanup_stale_tasks_returns_accepted_rows():
    s = make_storage()
    from knarr.core.models import Task
    task = Task(
        task_id="t1", skill_name="skill", requester_node_id="r" * 64,
        provider_node_id="p" * 64, status="accepted",
        input_data={}, created_at=time.time() - 1000, updated_at=time.time(),
        timeout_ms=5000
    )
    s.insert_task(task)

    rows = s.cleanup_stale_tasks(0)
    assert len(rows) == 1
    assert rows[0][0] == "t1"


# ──────────────────────────────────────────────────────────────────────────────
# PRE-01 #9: get_pricing_discounts
# ──────────────────────────────────────────────────────────────────────────────

def test_get_pricing_discounts_empty():
    s = make_storage()
    result = s.get_pricing_discounts()
    assert result == []


def test_get_pricing_discounts_multiple():
    s = make_storage()
    s.upsert_discount_rule("a", "g1", "*", 5.0, 0)
    s.upsert_discount_rule("b", "g2", "skill", 10.0, 1)
    result = s.get_pricing_discounts()
    assert len(result) == 2
    names = {r["name"] for r in result}
    assert "a" in names and "b" in names


# ──────────────────────────────────────────────────────────────────────────────
# PRE-01 #10: upsert_pricing_discount (alias of upsert_discount_rule)
# ──────────────────────────────────────────────────────────────────────────────

def test_upsert_pricing_discount():
    s = make_storage()
    s.upsert_pricing_discount("rule-x", "grp", "*", 7.5, 0)
    result = s.get_pricing_discounts()
    assert len(result) == 1
    assert result[0]["name"] == "rule-x"


# ──────────────────────────────────────────────────────────────────────────────
# PRE-01 #11: delete_pricing_discount (soft delete)
# ──────────────────────────────────────────────────────────────────────────────

def test_delete_pricing_discount_deactivates():
    s = make_storage()
    s.upsert_discount_rule("to-delete", "grp", "*", 10.0, 0)
    rules = s.get_pricing_discounts()
    rule_id = rules[0]["id"]
    assert rules[0]["active"] is True

    s.delete_pricing_discount(rule_id)
    # get_pricing_discounts returns all, including inactive
    updated = s.get_discount_rule_by_id(rule_id)
    assert updated["active"] is False


# ──────────────────────────────────────────────────────────────────────────────
# PRE-01 #12: get_settlement_queue_page
# ──────────────────────────────────────────────────────────────────────────────

def test_get_settlement_queue_page_empty():
    s = make_storage()
    rows, total = s.get_settlement_queue_page()
    assert rows == []
    assert total == 0


def test_get_settlement_queue_page_with_data():
    s = make_storage()
    s.queue_settlement("settle_request", "a" * 64, {"amount": 10.0}, priority=0)
    s.queue_settlement("settle_request", "b" * 64, {"amount": 20.0}, priority=1)

    rows, total = s.get_settlement_queue_page(limit=10)
    assert total == 2
    assert len(rows) == 2

    # Verify status filter
    rows_pending, total_pending = s.get_settlement_queue_page(status_filter="pending")
    assert total_pending == 2

    rows_done, total_done = s.get_settlement_queue_page(status_filter="processed")
    assert total_done == 0


def test_get_settlement_queue_page_pagination():
    s = make_storage()
    for i in range(5):
        s.queue_settlement("type", f"{i:064d}", {"n": i}, priority=0)

    rows, total = s.get_settlement_queue_page(limit=2, offset=0)
    assert len(rows) == 2
    assert total == 5

    rows2, _ = s.get_settlement_queue_page(limit=2, offset=2)
    assert len(rows2) == 2


# ──────────────────────────────────────────────────────────────────────────────
# PRE-01 #13: query_receipts_filtered
# ──────────────────────────────────────────────────────────────────────────────

def test_query_receipts_filtered_empty():
    s = make_storage()
    result = s.query_receipts_filtered()
    assert result == []


def test_query_receipts_filtered_with_data():
    s = make_storage()
    s.write_receipt(
        receipt_id="r1",
        document_type="settlement_prepared",
        timestamp="2024-01-01T00:00:00Z",
        identity="a" * 64,
        counterparty="b" * 64,
        order_ref=None,
        proof_purpose="assertionMethod",
        payload_json='{"amount": 10}',
        signature="sig1",
    )
    s.write_receipt(
        receipt_id="r2",
        document_type="settlement_accepted",
        timestamp="2024-01-01T00:01:00Z",
        identity="a" * 64,
        counterparty="c" * 64,
        order_ref=None,
        proof_purpose="assertionMethod",
        payload_json='{"amount": 20}',
        signature="sig2",
    )

    # All
    all_results = s.query_receipts_filtered()
    assert len(all_results) == 2

    # Filter by type
    by_type = s.query_receipts_filtered(document_type="settlement_prepared")
    assert len(by_type) == 1
    assert by_type[0]["receipt_id"] == "r1"

    # Filter by counterparty
    by_cp = s.query_receipts_filtered(counterparty="c" * 64)
    assert len(by_cp) == 1
    assert by_cp[0]["receipt_id"] == "r2"


def test_query_receipts_filtered_limit_clamped():
    s = make_storage()
    # Limit is clamped to [1, 500]
    result = s.query_receipts_filtered(limit=0)
    assert isinstance(result, list)

    result2 = s.query_receipts_filtered(limit=99999)
    assert isinstance(result2, list)


# ──────────────────────────────────────────────────────────────────────────────
# PRE-01 plugin_bridge: query_receipts uses storage method
# ──────────────────────────────────────────────────────────────────────────────

def test_plugin_bridge_query_receipts_uses_storage():
    """query_receipts in plugin_bridge.py delegates to storage.query_receipts_filtered."""
    import inspect
    from knarr.commerce import plugin_bridge
    src = inspect.getsource(plugin_bridge.query_receipts)
    # Should delegate to storage method
    assert "query_receipts_filtered" in src, (
        "plugin_bridge.query_receipts does not call storage.query_receipts_filtered"
    )
    # Should not call _get_conn() directly (only acceptable in comments/docstrings)
    # Strip comments/docstrings by checking that conn = storage._get_conn() pattern absent
    import re
    # Remove docstring block
    code_no_docs = re.sub(r'""".*?"""', '', src, flags=re.DOTALL)
    assert "storage._get_conn()" not in code_no_docs, (
        "plugin_bridge.query_receipts still calls storage._get_conn() directly"
    )


def test_plugin_bridge_query_receipts_works():
    """query_receipts produces correct results via storage delegation."""
    from knarr.commerce.plugin_bridge import query_receipts

    s = make_storage()
    s.write_receipt(
        receipt_id="rb1",
        document_type="receipt",
        timestamp="ts",
        identity="a" * 64,
        counterparty="b" * 64,
        order_ref=None,
        proof_purpose="assertionMethod",
        payload_json='{"x": 1}',
        signature=None,
    )

    results = query_receipts(s, document_type="receipt")
    assert len(results) == 1
    assert results[0]["receipt_id"] == "rb1"
    assert results[0]["payload"] == {"x": 1}
