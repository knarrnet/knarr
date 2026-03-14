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
    # DHTNode.__init__ calls asyncio.get_event_loop(); must run in async context.
    # Bind to localhost, advertise a fake public IP.
    node = DHTNode("127.0.0.1", 9000, advertise_host="1.2.3.4")
    assert node.node_info.host == "1.2.3.4"
    assert node._bind_host == "127.0.0.1"


@pytest.mark.asyncio
async def test_upnp_preserves_explicit_advertise_host():
    """BUG-006: explicit advertise_host overrides auto-detection at construction.

    UPnP was moved to the 02-upnp plugin in v0.41.0 (no longer in DHTNode).
    The advertise_host is captured in node_info.host at DHTNode.__init__ time,
    before any plugin runs, so it cannot be overwritten by a plugin.
    """
    # DHTNode.__init__ calls asyncio.get_event_loop(); must run in async context.
    config = {"node": {"advertise_host": "my.hostname.org"}, "network": {"upnp": True}}
    node = DHTNode("127.0.0.1", 9000, advertise_host="my.hostname.org", config=config)
    assert node.node_info.host == "my.hostname.org"
