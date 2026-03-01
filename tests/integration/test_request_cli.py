import pytest
import asyncio
import sys
import os
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_request_cli_integration():
    # 1. Start a provider node programmatically
    port = 9400
    node = DHTNode("127.0.0.1", port)
    await node.start()
    
    async def echo_handler(data):
        return data
        
    node.register_handler("echo-cli", echo_handler)
    await node.announce({
        "name": "echo-cli",
        "version": "1.0.0",
        "description": "d",
        "tags": ["cli"],
        "input_schema": {"text": "string"},
        "output_schema": {"text": "string"}
    })
    
    try:
        # 2. Run CLI request as subprocess
        env = os.environ.copy()

        cmd = [
            sys.executable, "-m", "knarr.cli.main", "request",
            "--bootstrap", f"127.0.0.1:{port}",
            "--skill", "echo-cli",
            "--input", '{"text": "cli-request-works"}'
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        
        output = stdout.decode()
        assert "Status: completed" in output
        assert "text: cli-request-works" in output
        assert proc.returncode == 0
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_request_cli_json():
    # 1. Start a provider node
    port = 9401
    node = DHTNode("127.0.0.1", port)
    await node.start()
    
    async def echo_handler(data):
        return data
        
    node.register_handler("echo-json", echo_handler)
    await node.announce({
        "name": "echo-json",
        "version": "1.0.0",
        "description": "d",
        "tags": ["cli"],
        "input_schema": {"text": "string"},
        "output_schema": {"text": "string"}
    })
    
    try:
        # 2. Run CLI request with --json
        env = os.environ.copy()

        cmd = [
            sys.executable, "-m", "knarr.cli.main", "request",
            "--bootstrap", f"127.0.0.1:{port}",
            "--skill", "echo-json",
            "--input", '{"text": "json-works"}',
            "--json"
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        
        # Verify JSON output
        import json
        res_data = json.loads(stdout.decode())
        assert res_data["status"] == "completed"
        assert res_data["output_data"]["text"] == "json-works"
        assert "task_id" in res_data
        assert proc.returncode == 0
        
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_request_cli_invalid_input_type():
    # Run CLI request with a string instead of an object
    env = os.environ.copy()

    cmd = [
        sys.executable, "-m", "knarr.cli.main", "request",
        "--bootstrap", "127.0.0.1:9000",
        "--skill", "echo",
        "--input", '"just a string"'
    ]
    
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env
    )
    
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
    
    assert b"Invalid --input: must be a JSON object" in stderr
    assert proc.returncode != 0


