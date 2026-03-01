import pytest
from unittest.mock import MagicMock, patch
from knarr.dht.upnp import UPnPManager

def test_upnp_discover_no_gateway():
    with patch("miniupnpc.UPnP") as mock_cls:
        mock_upnp = MagicMock()
        mock_cls.return_value = mock_upnp
        mock_upnp.discover.return_value = 0
        
        manager = UPnPManager()
        ip = manager.discover_and_map(9000)
        assert ip == ""
        assert manager.external_ip == ""

def test_upnp_discover_and_map_success():
    with patch("miniupnpc.UPnP") as mock_cls:
        mock_upnp = MagicMock()
        mock_cls.return_value = mock_upnp
        mock_upnp.discover.return_value = 1
        mock_upnp.externalipaddress.return_value = "1.2.3.4"
        mock_upnp.lanaddr = "192.168.1.10"
        
        manager = UPnPManager()
        ip = manager.discover_and_map(9000, 9001)
        
        assert ip == "1.2.3.4"
        assert manager.external_ip == "1.2.3.4"
        mock_upnp.selectigd.assert_called()
        
        # Verify mappings
        assert mock_upnp.addportmapping.call_count == 2
        # First call: protocol port
        args1 = mock_upnp.addportmapping.call_args_list[0]
        assert args1[0][0] == 9000
        assert args1[0][3] == 9000
        assert "Knarr protocol" in args1[0][4]
        
        # Second call: sidecar port
        args2 = mock_upnp.addportmapping.call_args_list[1]
        assert args2[0][0] == 9001
        assert args2[0][3] == 9001
        assert "Knarr sidecar" in args2[0][4]

def test_upnp_cleanup():
    with patch("miniupnpc.UPnP") as mock_cls:
        mock_upnp = MagicMock()
        mock_cls.return_value = mock_upnp
        mock_upnp.discover.return_value = 1
        
        manager = UPnPManager()
        manager.discover_and_map(9000)
        
        manager.cleanup()
        mock_upnp.deleteportmapping.assert_called_with(9000, "TCP")

def test_upnp_renew():
    with patch("miniupnpc.UPnP") as mock_cls:
        mock_upnp = MagicMock()
        mock_cls.return_value = mock_upnp
        mock_upnp.discover.return_value = 1
        mock_upnp.lanaddr = "192.168.1.10"
        
        manager = UPnPManager()
        manager.discover_and_map(9000)
        
        mock_upnp.addportmapping.reset_mock()
        manager.renew()
        
        mock_upnp.addportmapping.assert_called_once()
        args = mock_upnp.addportmapping.call_args[0]
        assert args[0] == 9000

def test_upnp_discover_exception_handled():
    with patch("miniupnpc.UPnP") as mock_cls:
        mock_upnp = MagicMock()
        mock_cls.return_value = mock_upnp
        mock_upnp.discover.side_effect = Exception("Boom")
        
        manager = UPnPManager()
        ip = manager.discover_and_map(9000)
        assert ip == ""
