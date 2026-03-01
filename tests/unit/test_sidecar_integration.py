import pytest
import os
import shutil
import asyncio
from knarr.dht.node import DHTNode
from knarr.dht.sidecar import TaskContext

@pytest.fixture
def asset_dir(tmp_path):
    d = tmp_path / "assets"
    d.mkdir()
    return str(d)

@pytest.mark.asyncio
async def test_sidecar_startup_and_shutdown(asset_dir):
    config = {
        "node": {"sidecar_port": 19876},
        "sidecar": {"asset_dir": asset_dir}
    }
    node = DHTNode("127.0.0.1", 0, config=config)

    await node.start()
    assert node._sidecar is not None
    assert node._sidecar_port > 0
    assert node.node_info.port > 0

    # Verify sidecar port in announce
    await node.announce({
        "name": "test-skill",
        "version": "1.0.0",
        "description": "d",
        "tags": ["t"],
        "input_schema": {},
        "output_schema": {}
    })

    # Query back to verify sidecar_port stored
    results = await node.query("name", "test-skill")
    assert len(results) == 1
    assert results[0]["sidecar_port"] == node._sidecar_port

    await node.stop()

@pytest.mark.asyncio
async def test_sidecar_disabled_when_port_zero(asset_dir):
    """P8A1-003: sidecar_port=0 must NOT start a sidecar."""
    config = {
        "node": {"sidecar_port": 0},
        "sidecar": {"asset_dir": asset_dir}
    }
    node = DHTNode("127.0.0.1", 0, config=config)
    await node.start()
    assert node._sidecar is None
    assert node._sidecar_port == 0
    await node.stop()

@pytest.mark.asyncio
async def test_task_context_dispatch(asset_dir):
    # Use a real port so sidecar starts and _asset_dir is set
    config = {
        "node": {"sidecar_port": 19877},
        "sidecar": {"asset_dir": asset_dir}
    }
    node = DHTNode("127.0.0.1", 0, config=config)
    await node.start()

    # Create asset
    with open(os.path.join(asset_dir, "test.txt"), "w") as f:
        f.write("content")

    received_ctx = None

    async def handler(data, ctx: TaskContext):
        nonlocal received_ctx
        received_ctx = ctx
        return data

    node.register_handler("test-ctx", handler)
    from knarr.core.models import SkillSheet
    node._own_skills["test-ctx"] = SkillSheet("test-ctx", "1.0", "d", [], {}, {})

    res = await node.request_task(
        node.node_info.node_id, "127.0.0.1", node.node_info.port,
        "test-ctx", {"foo": "bar"}
    )

    if res.status == "failed":
        print(f"DEBUG: Task failed with error: {res.error}")

    assert res.status == "completed"
    assert received_ctx is not None
    assert isinstance(received_ctx, TaskContext)
    assert received_ctx._asset_dir == asset_dir

    await node.stop()

@pytest.mark.asyncio
async def test_auto_resolve_uri(asset_dir):
    # Use a real port so sidecar starts and _asset_dir is set
    config = {
        "node": {"sidecar_port": 19878},
        "sidecar": {"asset_dir": asset_dir}
    }
    node = DHTNode("127.0.0.1", 0, config=config)
    await node.start()

    # Create asset
    ctx = TaskContext(asset_dir)
    h = ctx.store_asset(b"content")
    uri = f"knarr-asset://{h}"

    received_data = None

    async def handler(data):
        nonlocal received_data
        received_data = data
        return {}

    node.register_handler("test-resolve", handler)
    from knarr.core.models import SkillSheet
    node._own_skills["test-resolve"] = SkillSheet("test-resolve", "1.0", "d", [], {}, {})

    await node.request_task(
        node.node_info.node_id, "127.0.0.1", node.node_info.port,
        "test-resolve", {"file": uri}
    )

    assert received_data is not None
    # Should be resolved to absolute path
    expected_path = os.path.join(asset_dir, h)
    assert received_data["file"] == expected_path
    assert os.path.exists(received_data["file"])

    await node.stop()

@pytest.mark.asyncio
async def test_auto_resolve_rejects_traversal(asset_dir):
    """P8A1-001: knarr-asset://../../etc/passwd must NOT be resolved."""
    config = {
        "node": {"sidecar_port": 19879},
        "sidecar": {"asset_dir": asset_dir}
    }
    node = DHTNode("127.0.0.1", 0, config=config)
    await node.start()

    received_data = None

    async def handler(data):
        nonlocal received_data
        received_data = data
        return {}

    node.register_handler("test-traversal", handler)
    from knarr.core.models import SkillSheet
    node._own_skills["test-traversal"] = SkillSheet("test-traversal", "1.0", "d", [], {}, {})

    # Send a path traversal URI — should NOT be resolved
    await node.request_task(
        node.node_info.node_id, "127.0.0.1", node.node_info.port,
        "test-traversal", {"file": "knarr-asset://../../etc/passwd"}
    )

    assert received_data is not None
    # The traversal URI must be passed through unresolved
    assert received_data["file"] == "knarr-asset://../../etc/passwd"

    await node.stop()
