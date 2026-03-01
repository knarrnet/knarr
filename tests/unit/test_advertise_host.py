import socket
from unittest.mock import patch, MagicMock
from knarr.cli.config import detect_advertise_host, is_private_ip
from knarr.dht.node import DHTNode
import pytest

def test_detect_advertise_host():
    ip = detect_advertise_host()
    # Should be a string (can be empty if no network, but on CI usually not)
    assert isinstance(ip, str)

def test_is_private_ip():
    assert is_private_ip("127.0.0.1") == True
    assert is_private_ip("10.0.0.1") == True
    assert is_private_ip("192.168.1.1") == True
    assert is_private_ip("172.16.0.1") == True
    assert is_private_ip("172.31.255.255") == True
    assert is_private_ip("8.8.8.8") == False
    assert is_private_ip("203.0.113.1") == False

@pytest.mark.asyncio
async def test_dht_node_advertise_host():
    # Bind to localhost, advertise a fake public IP
    node = DHTNode("127.0.0.1", 9000, advertise_host="1.2.3.4")
    assert node.node_info.host == "1.2.3.4"
    assert node._bind_host == "127.0.0.1"

    await node.start()
    try:
        # Check NodeInfo in message
        assert node.node_info.host == "1.2.3.4"
    finally:
        await node.stop()


@pytest.mark.asyncio
async def test_upnp_preserves_explicit_advertise_host():
    """BUG-006: UPnP must not overwrite explicitly configured advertise_host."""
    mock_manager = MagicMock()
    mock_manager.discover_and_map.return_value = "5.6.7.8"

    config = {"node": {"advertise_host": "my.hostname.org"}, "network": {"upnp": True}}
    node = DHTNode("127.0.0.1", 9000, advertise_host="my.hostname.org", config=config)
    assert node.node_info.host == "my.hostname.org"

    with patch("knarr.dht.node.UPnPManager", return_value=mock_manager):
        await node.start()
    try:
        # UPnP discovered 5.6.7.8 but should NOT override the explicit hostname
        assert node.node_info.host == "my.hostname.org"
        mock_manager.discover_and_map.assert_called_once()
    finally:
        await node.stop()
