from knarr.dht.storage import Storage


def test_hold_balance_accumulates():
    storage = Storage(":memory:")
    storage.get_or_create_ledger_entry("a" * 64, 0.0, 0.3)
    storage.hold_balance("a" * 64, 2.5)
    storage.hold_balance("a" * 64, 1.5)
    entry = storage.get_or_create_ledger_entry("a" * 64, 0.0, 0.3)
    assert entry.held_balance == 4.0
    storage.close()


def test_release_held_moves_amount_into_balance():
    storage = Storage(":memory:")
    storage.get_or_create_ledger_entry("a" * 64, 0.0, 0.3)
    storage.hold_balance("a" * 64, 5.0)
    storage.release_held("a" * 64, 3.0)
    entry = storage.get_or_create_ledger_entry("a" * 64, 0.0, 0.3)
    assert entry.held_balance == 2.0
    assert entry.balance == 3.0
    storage.close()


def test_return_held_only_reduces_hold():
    storage = Storage(":memory:")
    # A1.2 security rule: get_or_create_ledger_entry always stores balance=0.0 in the DB
    # regardless of the initial_balance argument. Use 0.0 to match the actual stored value.
    storage.get_or_create_ledger_entry("a" * 64, 0.0, 0.3)
    storage.hold_balance("a" * 64, 4.0)
    storage.return_held("a" * 64, 2.5)
    entry = storage.get_or_create_ledger_entry("a" * 64, 0.0, 0.3)
    assert entry.held_balance == 1.5
    assert entry.balance == 0.0  # return_held does not credit balance (unlike release_held)
    storage.close()


def test_release_and_return_floor_at_zero():
    storage = Storage(":memory:")
    storage.get_or_create_ledger_entry("a" * 64, 0.0, 0.3)
    storage.hold_balance("a" * 64, 1.0)
    storage.release_held("a" * 64, 5.0)
    storage.return_held("a" * 64, 5.0)
    entry = storage.get_or_create_ledger_entry("a" * 64, 0.0, 0.3)
    assert entry.held_balance == 0.0
    assert entry.balance == 1.0
    storage.close()


def test_ledger_entry_exposes_held_balance():
    storage = Storage(":memory:")
    storage.get_or_create_ledger_entry("a" * 64, 0.0, 0.3)
    storage.hold_balance("a" * 64, 2.0)
    entry = storage.get_or_create_ledger_entry("a" * 64, 0.0, 0.3)
    assert hasattr(entry, "held_balance")
    assert entry.held_balance == 2.0
    storage.close()
