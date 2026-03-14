import asyncio
import time
import pytest
from knarr.dht.node import DHTNode
from knarr.core.models import NodeInfo, SkillSheet, Policy

@pytest.mark.asyncio
async def test_get_node_info_returns_expected_fields():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        info = node.get_node_info()
        assert info["node_id"] == node.node_info.node_id
        assert info["host"] == "127.0.0.1"
        assert info["port"] > 0
        assert isinstance(info["public_key"], str)
        assert len(info["public_key"]) == 64
        assert info["uptime_seconds"] >= 0
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_get_peer_summary_includes_activity():
    """v0.14.0: _missed_heartbeats replaced by implicit heartbeat (_peer_last_activity)."""
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        # Add a peer
        peer = NodeInfo("p1", "1.2.3.4", 9000)
        node.storage.upsert_peer(peer)

        summary = node.get_peer_summary()
        assert len(summary) == 1
        assert summary[0]["node_id"] == "p1"
        assert summary[0]["missed_heartbeats"] == 0  # Legacy field, always 0 now
        assert summary[0]["last_seen"] > 0
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_get_skill_summary_includes_stats():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    node.register_handler("test-skill", lambda d: d)
    await node.announce({
        "name": "test-skill", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}, "price": 2.5, "max_input_size": 1024
    })
    
    try:
        # Complete a task to generate stats
        from knarr.core.messages import TaskRequest
        req = node._sign(TaskRequest(
            task_id="t1", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999,
            skill_name="test-skill", input_data={}
        ))
        await node._process_message(req)
        
        summary = node.get_skill_summary()
        assert len(summary) == 1
        assert summary[0]["name"] == "test-skill"
        assert summary[0]["price"] == 2.5
        assert summary[0]["max_input_size"] == 1024
        assert summary[0]["tasks_completed"] == 1
        assert summary[0]["avg_wall_time_ms"] >= 0
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_get_economy_summary_returns_ledger():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        node.storage.get_or_create_ledger_entry("key1", 5.0)
        
        summary = node.get_economy_summary()
        assert "peers" in summary
        assert "summary" in summary
        assert len(summary["peers"]) == 1
        assert summary["peers"][0]["public_key"] == "key1"
        # A1.2: new entries start at balance=0.0 regardless of initial_balance param
        assert summary["peers"][0]["balance"] == 0.0
        assert summary["peers"][0]["status"] in ("green", "amber", "red")
        assert summary["summary"]["net_position"] == 0.0
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_get_task_feed_respects_limit():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    node.register_handler("s", lambda d: d)
    await node.announce({
        "name": "s", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}
    })
    
    try:
        from knarr.core.messages import TaskRequest
        for i in range(5):
            req = node._sign(TaskRequest(
                task_id=f"t{i}", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999,
                skill_name="s", input_data={}
            ))
            await node._process_message(req)
            await asyncio.sleep(0.01)
            
        feed = node.get_task_feed(limit=3)
        assert len(feed) == 3
        assert feed[0]["task_id"] == "t4"
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_get_demand_summary_returns_entries():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        node.storage.record_demand("name", "missing")
        
        summary = node.get_demand_summary()
        assert len(summary) == 1
        assert summary[0]["value"] == "missing"
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_get_writer_queue_depth():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        assert node.get_writer_queue_depth() == 0
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_get_queue_status_returns_expected_fields():
    node = DHTNode("127.0.0.1", 0, config={"node": {"task_slots": 2}})
    await node.start()
    try:
        status = node.get_queue_status()
        assert status["task_slots"] == 2
        assert "active_workers" in status
        assert "queue_depth" in status
        assert status["queue_max"] == 4
        assert 0 <= status["load"] <= 10
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_get_skill_stats_percentiles():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    node.register_handler("test", lambda d: d)
    
    try:
        from knarr.core.models import Task
        for i, ms in enumerate([100, 200, 300, 400, 500]):
            # Insert task first
            t = Task(f"t{i}", "test", "r", node.node_info.node_id, "submitted", {})
            node.storage.insert_task(t)
            # Update status with telemetry
            node.storage.update_task_status(f"t{i}", "completed", {}, None, 0, ms)
            
        stats = node.get_skill_stats("test")
        # p50 of [100, 200, 300, 400, 500] is 300
        # p95 is 500
        assert stats["p50_ms"] == 300
        assert stats["p95_ms"] == 500
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_get_peer_summary_includes_load():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        from knarr.core.models import NodeInfo
        node.storage.upsert_peer(NodeInfo("p1", "1.2.3.4", 9000))
        await node._enqueue_write(node.storage.update_peer_load, "p1", 5)
        
        # Give writer loop time
        await asyncio.sleep(0.1)
        
        summary = node.get_peer_summary()
        peer = next(p for p in summary if p["node_id"] == "p1")
        assert peer["load"] == 5
    finally:
        await node.stop()


@pytest.mark.asyncio
async def test_secret_injection():
    """G-9: Secrets are injected into input_data, caller values take precedence."""
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        # Manually set secrets via SecretsManager (node._inject_secrets delegates to _secrets_mgr)
        node._secrets_mgr._secrets = {"echo": {"api_key": "secret123", "default_mode": "fast"}}

        # Injection adds missing keys
        result = node._inject_secrets("echo", {"text": "hello"})
        assert result["text"] == "hello"
        assert result["api_key"] == "secret123"
        assert result["default_mode"] == "fast"

        # Caller values take precedence
        result = node._inject_secrets("echo", {"text": "hello", "api_key": "caller-key"})
        assert result["api_key"] == "caller-key"

        # No secrets for unknown skill
        result = node._inject_secrets("unknown", {"text": "hello"})
        assert result == {"text": "hello"}
    finally:
        await node.stop()


@pytest.mark.asyncio
async def test_secrets_persist_and_load(tmp_path):
    """G-9: Secrets persist to vault and reload correctly."""
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        node.set_secret("echo", "api_key", "test-value")
        assert node._secrets["echo"]["api_key"] == "test-value"

        # Vault stores the value (if vault is available)
        if node._vault:
            assert node._vault.get("echo", "api_key") == "test-value"

        # Reload from vault
        node._secrets = {}
        node.load_secrets()
        if node._vault:
            assert node._secrets["echo"]["api_key"] == "test-value"

        # Delete
        node.delete_secret("echo", "api_key")
        assert "echo" not in node._secrets
    finally:
        await node.stop()


@pytest.mark.asyncio
async def test_call_local_timeout():
    """CR-001: call_local raises TimeoutError on slow handler."""
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        def slow_handler(data):
            import time
            time.sleep(5)
            return {"result": "done"}
        node.register_handler("slow-test", slow_handler)

        with pytest.raises(asyncio.TimeoutError):
            await node.call_local("slow-test", {}, timeout_ms=200)
    finally:
        await node.stop()


@pytest.mark.asyncio
async def test_call_local_completes_within_timeout():
    """CR-001: call_local returns normally when handler is fast."""
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        def fast_handler(data):
            return {"text": data.get("text", "")}
        node.register_handler("fast-test", fast_handler)

        result = await node.call_local("fast-test", {"text": "hello"}, timeout_ms=5000)
        assert result["text"] == "hello"
    finally:
        await node.stop()
