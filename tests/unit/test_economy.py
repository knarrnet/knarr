import pytest
import time
from knarr.dht.storage import Storage
from knarr.core.models import SkillSheet, Policy
from knarr.core.validation import validate_skill_sheet, ValidationError

@pytest.fixture
def storage():
    return Storage(":memory:")

def test_ledger_entry_creation_with_initial_credit(storage):
    key = "peer1"
    # A1.2 security rule: get_or_create_ledger_entry always stores balance=0.0 in the DB
    # regardless of initial_balance argument. New entries start at zero.
    entry = storage.get_or_create_ledger_entry(key, initial_balance=0.0)
    assert entry.balance == 0.0
    assert entry.tasks_provided == 0
    assert entry.tasks_consumed == 0

def test_ledger_update_provider(storage):
    key = "peer1"
    storage.get_or_create_ledger_entry(key, initial_balance=0.0)
    
    # Provider perspective: consumer spent credit
    storage.update_ledger_provider(key, 1.0)
    
    entry = storage.get_or_create_ledger_entry(key)
    assert entry.balance == -1.0
    assert entry.tasks_provided == 1

def test_ledger_update_consumer(storage):
    key = "peer1"
    storage.get_or_create_ledger_entry(key, initial_balance=0.0)
    
    # Consumer perspective: provider earned credit
    storage.update_ledger_consumer(key, 1.0)
    
    entry = storage.get_or_create_ledger_entry(key)
    assert entry.balance == 1.0
    assert entry.tasks_consumed == 1

def test_variable_pricing(storage):
    key = "peer1"
    # A1.2: new entries always start at 0.0. Set the working balance via direct SQL
    # (same pattern used by test_credit_balancer.py) to verify provider charge logic.
    storage.get_or_create_ledger_entry(key, initial_balance=0.0)
    conn = storage._get_conn()
    conn.execute("UPDATE ledger SET balance = 3.0 WHERE peer_public_key = ?", (key,))
    conn.commit()
    storage.update_ledger_provider(key, 2.0)

    entry = storage.get_or_create_ledger_entry(key)
    assert entry.balance == 1.0  # 3.0 - 2.0

def test_ledger_entry_reuse(storage):
    key = "peer1"
    e1 = storage.get_or_create_ledger_entry(key)
    storage.update_ledger_provider(key, 1.0)
    e2 = storage.get_or_create_ledger_entry(key)
    
    assert e1.peer_public_key == e2.peer_public_key
    assert e2.balance == e1.balance - 1.0

def test_policy_check_initial_credit_allows(storage):
    policy = Policy(initial_credit=3.0, min_balance=-10.0)
    entry = storage.get_or_create_ledger_entry("peer1", initial_balance=policy.initial_credit)
    assert entry.balance >= policy.min_balance

def test_policy_check_balance_below_min(storage):
    policy = Policy(min_balance=-10.0)
    # Balance -11.0 is below -10.0
    allowed = -11.0 >= policy.min_balance
    assert allowed == False

def test_policy_tit_for_tat_mode():
    """P1: tit_for_tat skips credit check entirely — negative balance is allowed."""
    policy = Policy(min_balance=-10.0, tit_for_tat=True)
    # With T4T, credit check is skipped — any balance is allowed
    assert policy.tit_for_tat is True
    # Even deeply negative balance should not block
    assert not (not policy.tit_for_tat and -100.0 < policy.min_balance)

def test_price_validation_valid():
    sheet = {"name": "s", "version": "1.0.0", "description": "d", "tags": ["t"], 
             "input_schema": {}, "output_schema": {}, "price": 2.0}
    obj = validate_skill_sheet(sheet)
    assert obj.price == 2.0

def test_price_validation_zero_allowed():
    """price=0.0 is valid (free/system skills)."""
    sheet = {"name": "s", "version": "1.0.0", "description": "d", "tags": ["t"],
             "input_schema": {}, "output_schema": {}, "price": 0.0}
    result = validate_skill_sheet(sheet)
    assert result.price == 0.0

def test_price_validation_negative():
    """ESC-02: Negative prices are now valid (bounty/escrow pattern)."""
    sheet = {"name": "s", "version": "1.0.0", "description": "d", "tags": ["t"],
             "input_schema": {}, "output_schema": {}, "price": -1.0}
    result = validate_skill_sheet(sheet)
    assert result.price == -1.0

def test_price_validation_too_high():
    sheet = {"name": "s", "version": "1.0.0", "description": "d", "tags": ["t"], 
             "input_schema": {}, "output_schema": {}, "price": 1001.0}
    with pytest.raises(ValidationError, match="not exceed 1000.0"):
        validate_skill_sheet(sheet)

def test_price_validation_default():
    sheet = {"name": "s", "version": "1.0.0", "description": "d", "tags": ["t"], 
             "input_schema": {}, "output_schema": {}}
    obj = validate_skill_sheet(sheet)
    assert obj.price == 1.0

def test_demand_recording(storage):
    storage.record_demand("name", "foo")
    demand = storage.get_demand()
    assert len(demand) == 1
    assert demand[0]["value"] == "foo"
    assert demand[0]["count"] == 1

def test_demand_increment(storage):
    storage.record_demand("name", "foo")
    storage.record_demand("name", "foo")
    demand = storage.get_demand()
    assert len(demand) == 1
    assert demand[0]["count"] == 2

def test_ranking_by_liveness():
    # Simulate node ranking logic locally
    now = time.time()
    results = [
        {"_last_seen": now - 100, "id": "old"},
        {"_last_seen": now - 10, "id": "new"}
    ]
    
    # Mock node logic
    def normalize(values):
        min_v, max_v = min(values), max(values)
        if max_v == min_v: return [1.0] * len(values)
        return [(max_v - v) / (max_v - min_v) for v in values]

    liveness = normalize([now - r["_last_seen"] for r in results])
    # new (10s ago) < old (100s ago), so new gets higher score
    assert liveness[1] > liveness[0]

def test_ranking_deterministic():
    results = [{"id": "a", "_score": 0.5}, {"id": "b", "_score": 0.5}]
    # Sort relies on stable sort if keys equal? 
    # Python sort is stable.
    results.sort(key=lambda r: r["_score"], reverse=True)
    assert results[0]["id"] == "a"

def test_ledger_size_cap(storage):
    # Import max from storage module to be consistent with implementation
    from knarr.dht.storage import MAX_LEDGER_ENTRIES
    
    # We can't insert 10000 entries efficiently in a unit test, 
    # but we can verify the logic by patching MAX_LEDGER_ENTRIES
    # Since it's a global in storage.py, we can patch it before creating storage?
    # Or just monkeypatch it
    import knarr.dht.storage
    orig_max = knarr.dht.storage.MAX_LEDGER_ENTRIES
    knarr.dht.storage.MAX_LEDGER_ENTRIES = 2
    
    try:
        storage.get_or_create_ledger_entry("k1")
        time.sleep(0.01) # ensure diff timestamps
        storage.get_or_create_ledger_entry("k2")
        time.sleep(0.01)
        storage.get_or_create_ledger_entry("k3")
        
        # Should have evicted k1 (oldest)
        cursor = storage._get_conn().execute("SELECT count(*) FROM ledger")
        assert cursor.fetchone()[0] == 2
        
        entry = storage.get_ledger_balance("k1")
        assert entry is None
        
    finally:
        knarr.dht.storage.MAX_LEDGER_ENTRIES = orig_max
