import pytest
import time
from knarr.dht.node import DHTNode
from knarr.core.models import Task
from knarr.cli.main import cmd_info

@pytest.mark.asyncio
async def test_get_reputation_summary_returns_expected_fields():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        # Insert a task and a ledger entry
        t = Task("t1", "s", "r", "p1", "completed", {})
        node.storage.insert_task(t, provider_public_key="pub1")
        node.storage.update_task_status("t1", "completed", wall_time_ms=100)
        node.storage.get_or_create_ledger_entry("pub1", 50.0)
        # quality_rating column is added lazily by update_receipt_quality —
        # ensure it exists before get_reputation_summary queries it.
        node.storage.update_receipt_quality("t1", 4)

        reps = node.get_reputation_summary()
        assert len(reps) == 1
        rep = reps[0]
        assert rep["provider_node_id"] == "p1"
        assert rep["provider_public_key"] == "pub1"
        # A1.2: new entries start at balance=0.0; initial_balance param ignored.
        assert rep["balance"] == 0.0
        assert rep["success_rate"] == 1.0
        assert rep["avg_wall_time_ms"] == 100
        assert "tasks_provided" in rep
        assert "tasks_consumed" in rep
        assert "total_tasks_30d" in rep
        assert "first_seen" in rep
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_get_diversification_info():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        node.storage.get_or_create_ledger_entry("key1", 10.0)
        node.storage.get_or_create_ledger_entry("key2", 10.0)
        node.storage.get_or_create_ledger_entry("key3", 10.0)
        
        info = node.get_diversification_info()
        assert info["unique_counterparties"] == 3
        assert "total_tasks_provided" in info
        assert "total_tasks_consumed" in info
    finally:
        await node.stop()

def test_cmd_info_reputation_flag(capsys):
    from knarr.dht.storage import Storage
    storage = Storage("test_info.db")
    try:
        storage.get_or_create_ledger_entry("pub1", 50.0)
        t = Task("t1", "s", "r", "p1", "completed", {})
        storage.insert_task(t, provider_public_key="pub1")
        storage.update_task_status("t1", "completed", wall_time_ms=100)
        storage.close()
        
        class Args:
            storage = "test_info.db"
            reputation = True
        
        cmd_info(Args())
        captured = capsys.readouterr()
        assert "Counterparties: 1 unique peers" in captured.out
        assert "Provider History" in captured.out
        assert "Ledger" in captured.out
    finally:
        import os
        if os.path.exists("test_info.db"):
            os.remove("test_info.db")
