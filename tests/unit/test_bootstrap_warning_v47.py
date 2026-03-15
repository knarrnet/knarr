import sys
from pathlib import Path
from unittest.mock import patch

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from knarr.dht.node import DHTNode


@pytest.mark.asyncio
async def test_start_warns_when_no_bootstrap_peers_configured():
    node = DHTNode(
        "127.0.0.1",
        0,
        storage_path=":memory:",
        config={"network": {"bootstrap": []}, "node": {"sidecar_port": 0}},
    )

    with patch("knarr.dht.node.logger.warning") as mock_warning:
        await node.start()
        await node.stop()

    messages = [call.args[0] for call in mock_warning.call_args_list]
    assert any(
        "No bootstrap peers configured — this node is isolated from the network" in message
        and "[network] bootstrap" in message
        for message in messages
    )


@pytest.mark.asyncio
async def test_no_bootstrap_warning_when_peers_configured():
    node = DHTNode(
        "127.0.0.1",
        0,
        storage_path=":memory:",
        config={"network": {"bootstrap": ["bootstrap1.knarr.network:9000"]}, "node": {"sidecar_port": 0}},
    )

    with patch("knarr.dht.node.logger.warning") as mock_warning:
        await node.start()
        await node.stop()

    messages = [call.args[0] for call in mock_warning.call_args_list]
    assert not any(
        "No bootstrap peers configured" in message
        for message in messages
    )
