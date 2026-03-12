from dataclasses import FrozenInstanceError

import pytest

from knarr.core.models import LedgerEntry, SkillSheet, Task


def test_ledger_entry_exposes_extended_fields():
    entry = LedgerEntry(
        peer_public_key="a" * 64,
        balance=1.0,
        prepaid=2.0,
        pub_tab=3.0,
        soft_limit=-5.0,
        hard_limit=-10.0,
        held_balance=4.0,
        credit_limit=5.0,
        trust=0.7,
    )
    assert entry.prepaid == 2.0
    assert entry.held_balance == 4.0
    assert entry.credit_limit == 5.0
    assert entry.trust == 0.7


def test_ledger_entry_is_frozen():
    entry = LedgerEntry(peer_public_key="a" * 64)
    with pytest.raises((AttributeError, FrozenInstanceError)):
        entry.balance = 99.0


def test_skill_sheet_round_trip_preserves_price_and_uri():
    skill = SkillSheet(
        name="Demo",
        version="1.0.0",
        description="desc",
        tags=["Tag"],
        input_schema={"text": "string"},
        output_schema={"result": "string"},
        uri="knarr:///demo@1.0.0",
        price=2.5,
    )
    clone = SkillSheet.from_dict(skill.to_dict())
    assert clone.name == "demo"
    assert clone.uri == "knarr:///demo@1.0.0"
    assert clone.price == 2.5


def test_task_to_dict_includes_timeout_ms():
    task = Task(
        task_id="task-1",
        skill_name="demo",
        requester_node_id="req",
        provider_node_id="prov",
        status="accepted",
        input_data={"x": 1},
        timeout_ms=1234,
    )
    assert task.to_dict()["timeout_ms"] == 1234
