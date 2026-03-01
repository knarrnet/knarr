import pytest
import asyncio
from knarr.dht.node import DHTNode
from knarr.core.models import SkillSheet, Task

@pytest.mark.asyncio
async def test_configurable_timeout_default():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    # Register slow handler
    async def slow_handler(d):
        await asyncio.sleep(0.2)
        return d
    node.register_handler("slow", slow_handler, slow=False)
    node._own_skills["slow"] = SkillSheet("slow", "1.0", "d", [], {}, {})
    
    # Request with 5000ms timeout
    print(f"DEBUG: Node port: {node.node_info.port}")
    res = await node.request_task(
        node.node_info.node_id, "127.0.0.1", node.node_info.port,
        "slow", {}, timeout_ms=5000
    )
    if res.status == "failed":
        print(f"DEBUG: {res.error}")
    assert res.status == "completed"
    
    await node.stop()

@pytest.mark.asyncio
async def test_configurable_timeout_capped():
    # Set max timeout to 100ms
    config = {"node": {"max_task_timeout": 0.1}}
    node = DHTNode("127.0.0.1", 0, config=config)
    await node.start()
    
    async def slow_handler(d):
        await asyncio.sleep(0.3)
        return d
    node.register_handler("slow", slow_handler, slow=True)
    node._own_skills["slow"] = SkillSheet("slow", "1.0", "d", [], {}, {})
    
    # Request with 500ms timeout. Should be capped to 100ms.
    # Handler sleeps 300ms, so it should timeout.
    res = await node.request_task(
        node.node_info.node_id, "127.0.0.1", node.node_info.port,
        "slow", {}, timeout_ms=500
    )
    
    # request_task handles async result waiting. If handler times out on provider,
    # provider sends TaskResult(failed, error=TIMEOUT).
    assert res.status == "failed"
    assert res.error["code"] == "TIMEOUT"
    
    await node.stop()

@pytest.mark.asyncio
async def test_configurable_timeout_zero_unlimited():
    # Set max timeout to 0 (unlimited)
    config = {"node": {"max_task_timeout": 0}}
    node = DHTNode("127.0.0.1", 0, config=config)
    await node.start()
    
    async def slow_handler(d):
        await asyncio.sleep(0.2)
        return d
    node.register_handler("slow", slow_handler, slow=False)
    node._own_skills["slow"] = SkillSheet("slow", "1.0", "d", [], {}, {})
    
    # Request with 2000ms timeout.
    # Previous hardcode was 300s. 0 means trust the requester's timeout.
    res = await node.request_task(
        node.node_info.node_id, "127.0.0.1", node.node_info.port,
        "slow", {}, timeout_ms=2000
    )
    assert res.status == "completed"
    
    await node.stop()
