import pytest
import asyncio
from knarr.dht.node import DHTNode
from knarr.core.models import Policy

@pytest.mark.asyncio
async def test_initial_credit_exhaustion():
    # Provider with strict policy
    policy = Policy(initial_credit=3.0, min_balance=0.0)
    provider = DHTNode("127.0.0.1", 9700, policy=policy)
    await provider.start()
    
    # Register simple handler
    async def handler(data): return data
    provider.register_handler("credit-test", handler)
    await provider.announce({
        "name": "credit-test",
        "version": "1.0.0",
        "description": "d",
        "tags": ["test"],
        "input_schema": {"a": "int"},
        "output_schema": {"a": "int"},
        "price": 1.0
    })
    
    consumer = DHTNode("127.0.0.1", 9701)
    await consumer.start()
    await consumer.join(["127.0.0.1:9700"])
    
    try:
        # 3 tasks allowed (3.0 credit, 1.0 price) -> Balances: 3.0 -> 2.0 -> 1.0 -> 0.0
        # 4th task: Balance 0.0. Min 0.0. 0.0 < 0.0 is False. Allowed. Post-exec: -1.0
        for i in range(4):
            res = await consumer.request_task(
                provider.node_info.node_id, "127.0.0.1", 9700,
                "credit-test", {"a": 1}
            )
            assert res.status == "completed"
            
        # 5th task: Balance -1.0. -1.0 < 0.0 is True. Rejected.
        res = await consumer.request_task(
            provider.node_info.node_id, "127.0.0.1", 9700,
            "credit-test", {"a": 1}
        )
        assert res.status == "failed"
        assert res.error["code"] == "INSUFFICIENT_CREDIT"
        
    finally:
        await provider.stop()
        await consumer.stop()

@pytest.mark.asyncio
async def test_credit_building():
    # A provides to B, B provides to A
    node_a = DHTNode("127.0.0.1", 9710, policy=Policy(initial_credit=0.0))
    node_b = DHTNode("127.0.0.1", 9711, policy=Policy(initial_credit=0.0))
    
    await node_a.start()
    await node_b.start()
    await node_b.join(["127.0.0.1:9710"])
    
    # Handlers
    async def handler(d): return d
    node_a.register_handler("skill-a", handler)
    await node_a.announce({
        "name": "skill-a", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}, "price": 1.0
    })
    
    node_b.register_handler("skill-b", handler)
    await node_b.announce({
        "name": "skill-b", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}, "price": 1.0
    })
    
    try:
        # B consumes from A -> A earns credit
        res = await node_b.request_task(
            node_a.node_info.node_id, "127.0.0.1", 9710, "skill-a", {}
        )
        assert res.status == "completed"
        
        # Check A's ledger for B (A provided to B)
        # A (provider) decremented B's balance. 0.0 - 1.0 = -1.0.
        b_key = node_b._signing_key.verify_key.encode().hex()
        entry_a = node_a.storage.get_ledger_balance(b_key)
        assert entry_a == -1.0 # B consumed 1.0, so balance is -1.0 (debt to A)
        
        # A consumes from B
        res = await node_a.request_task(
            node_b.node_info.node_id, "127.0.0.1", 9711, "skill-b", {}
        )
        assert res.status == "completed"
        
        a_key = node_a._signing_key.verify_key.encode().hex()
        
        # Check B's ledger for A (Checked after step 2)
        # Step 1: B consumed from A. B (consumer) incremented A's balance. 0.0 + 1.0 = 1.0.
        # Step 2: A consumed from B. B (provider) decremented A's balance. 1.0 - 1.0 = 0.0.
        entry_b = node_b.storage.get_ledger_balance(a_key)
        assert entry_b == 0.0
        
    finally:
        await node_a.stop()
        await node_b.stop()

@pytest.mark.asyncio
async def test_variable_pricing_exhaustion():
    policy = Policy(initial_credit=3.0, min_balance=0.0)
    provider = DHTNode("127.0.0.1", 9720, policy=policy)
    await provider.start()
    
    async def handler(d): return d
    provider.register_handler("expensive", handler)
    await provider.announce({
        "name": "expensive", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}, "price": 2.0
    })
    
    consumer = DHTNode("127.0.0.1", 9721)
    await consumer.start()
    
    try:
        # 1st: 3.0 - 2.0 = 1.0 left (check pass: 3.0 >= 0.0)
        res = await consumer.request_task(
            provider.node_info.node_id, "127.0.0.1", 9720, "expensive", {}
        )
        assert res.status == "completed"
        
        # 2nd: 1.0 - 2.0 = -1.0. (check pass: 1.0 >= 0.0)
        res = await consumer.request_task(
            provider.node_info.node_id, "127.0.0.1", 9720, "expensive", {}
        )
        assert res.status == "completed"
        
        # 3rd: Balance -1.0. (check fail: -1.0 < 0.0)
        res = await consumer.request_task(
            provider.node_info.node_id, "127.0.0.1", 9720, "expensive", {}
        )
        assert res.status == "failed"
        assert res.error["code"] == "INSUFFICIENT_CREDIT"
        
    finally:
        await provider.stop()
        await consumer.stop()

@pytest.mark.asyncio
async def test_demand_recording_on_zero_results():
    node = DHTNode("127.0.0.1", 9730)
    await node.start()
    
    try:
        results = await node.query("name", "nonexistent")
        assert len(results) == 0
        
        demand = node.storage.get_demand()
        assert len(demand) == 1
        assert demand[0]["value"] == "nonexistent"
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_price_in_query_results():
    provider = DHTNode("127.0.0.1", 9740)
    await provider.start()
    
    await provider.announce({
        "name": "priced", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}, "price": 2.5
    })
    
    consumer = DHTNode("127.0.0.1", 9741)
    await consumer.start()
    await consumer.join(["127.0.0.1:9740"])
    
    try:
        results = await consumer.query("name", "priced")
        assert len(results) == 1
        assert results[0]["skill_sheet"]["price"] == 2.5
        
    finally:
        await provider.stop()
        await consumer.stop()