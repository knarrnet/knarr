import pytest
import asyncio
import subprocess
import sys
import os
import time
from knarr.dht.node import DHTNode

@pytest.mark.asyncio
async def test_cli_query_integration():
    # 1. Start a DHTNode programmatically
    port = 9098
    node = DHTNode("127.0.0.1", port)
    await node.start()
    
    # Announce a skill
    await node.announce({
        "name": "cli-test-skill",
        "version": "1.0.0",
        "description": "testing cli query",
        "tags": ["cli"],
        "input_schema": {},
        "output_schema": {}
    })
    
    try:
        # 2. Run CLI query as subprocess
        env = os.environ.copy()

        cmd = [sys.executable, "-m", "knarr.cli.main", "query", "--bootstrap", f"127.0.0.1:{port}", "--name", "cli-test-skill"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        
        output = stdout.decode()
        assert "cli-test-skill" in output
        assert proc.returncode == 0
        
    finally:
        await node.stop()
