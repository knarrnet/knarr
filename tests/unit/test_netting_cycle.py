"""Tests for treasury netting cycle."""
from knarr.commerce.netting import run_netting_cycle
from unittest.mock import MagicMock

def test_netting_queues_settlement():
    node = MagicMock()
    node._config = {}
    node.node_info.node_id = "self_id"
    
    node.storage.get_all_ledger_entries.return_value = [
        {"peer_public_key": "peer1", "balance": 10.0}
    ]
    node._resolve_policy.return_value = (100.0, 0.0)
    node.storage.has_pending_settlement.return_value = False
    
    queued = run_netting_cycle(node)
    
    assert queued == 1
    node.storage.queue_settlement.assert_called_once()
    
def test_netting_below_threshold_skipped():
    node = MagicMock()
    node._config = {}
    
    node.storage.get_all_ledger_entries.return_value = [
        {"peer_public_key": "peer1", "balance": 50.0}
    ]
    node._resolve_policy.return_value = (100.0, 0.0)
    node.storage.has_pending_settlement.return_value = False
    
    queued = run_netting_cycle(node)
    assert queued == 0

def test_netting_min_amount_respected():
    node = MagicMock()
    node._config = {"settlement": {"min_settlement_amount": 50.0}}
    
    node.storage.get_all_ledger_entries.return_value = [
        {"peer_public_key": "peer1", "balance": 10.0}
    ]
    node._resolve_policy.return_value = (100.0, 0.0)
    node.storage.has_pending_settlement.return_value = False
    
    # Target 50, so amount = 40, which is < min 50
    queued = run_netting_cycle(node)
    assert queued == 0

def test_netting_dedup():
    node = MagicMock()
    node._config = {}
    
    node.storage.get_all_ledger_entries.return_value = [
        {"peer_public_key": "peer1", "balance": 10.0}
    ]
    node._resolve_policy.return_value = (100.0, 0.0)
    node.storage.has_pending_settlement.return_value = True
    
    queued = run_netting_cycle(node)
    assert queued == 0
