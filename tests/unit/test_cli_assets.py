import pytest
import os
import shutil
import asyncio
from knarr.cli.main import cmd_request, cmd_serve, upload_asset, download_asset
from knarr.dht.node import DHTNode
from knarr.dht.sidecar import TaskContext
from nacl.signing import SigningKey

@pytest.fixture
def asset_dir(tmp_path):
    d = tmp_path / "assets"
    d.mkdir()
    return str(d)

@pytest.mark.asyncio
async def test_cli_asset_roundtrip(asset_dir, capsys):
    # Setup provider with real sidecar port
    node = DHTNode("127.0.0.1", 0, config={"node": {"sidecar_port": 19880}, "sidecar": {"asset_dir": asset_dir}})
    await node.start()

    # Register handler
    async def handler(data):
        return {"output_file": data["input_file"]}
    node.register_handler("echo-file", handler)

    # Create input file
    input_file = os.path.join(asset_dir, "input.txt")
    with open(input_file, "wb") as f:
        f.write(b"cli-test")

    # Upload manually to test helper
    h = await upload_asset("127.0.0.1", node._sidecar_port, b"cli-test", node._signing_key)
    assert len(h) == 64

    # Download manually to test helper
    data = await download_asset("127.0.0.1", node._sidecar_port, h, node._signing_key)
    assert data == b"cli-test"

    await node.stop()

@pytest.mark.asyncio
async def test_cli_at_file_upload(asset_dir, monkeypatch):
    # Setup provider with real sidecar port
    node = DHTNode("127.0.0.1", 0, config={"node": {"sidecar_port": 19881}, "sidecar": {"asset_dir": asset_dir}})
    await node.start()

    async def handler(data):
        # Verify input is resolved URI
        assert data["file"].startswith("knarr-asset://")
        return {"result": "ok"}
    node.register_handler("file-skill", handler)
    from knarr.core.models import SkillSheet
    node._own_skills["file-skill"] = SkillSheet("file-skill", "1.0", "d", [], {}, {})

    # Create local file to upload
    local_file = "test_upload.txt"
    with open(local_file, "wb") as f:
        f.write(b"upload-me")

    try:
        class Args:
            skill = "file-skill"
            input_json = f'{{"file": "@{local_file}"}}'
            bootstrap = f"127.0.0.1:{node.node_info.port}"
            direct = None
            port = 0
            timeout = 5.0
            json = True
            output_dir = None
            log_level = "CRITICAL"

        # Mock sys.exit to catch errors
        monkeypatch.setattr("sys.exit", lambda x: None)

        await cmd_request(Args())

        # Verify asset stored
        import hashlib
        h = hashlib.sha256(b"upload-me").hexdigest()
        assert os.path.exists(os.path.join(asset_dir, h))

    finally:
        if os.path.exists(local_file):
            os.remove(local_file)
        await node.stop()
