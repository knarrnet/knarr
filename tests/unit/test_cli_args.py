import pytest
from knarr.cli import main as cli_module
import sys

def test_cli_parse_serve(monkeypatch):
    test_args = ["knarr", "serve", "--port", "9000", "--host", "127.0.0.1", "--bootstrap", "peer1:9000"]
    monkeypatch.setattr(sys, "argv", test_args)
    
    args_captured = []
    async def mock_serve(args):
        args_captured.append(args)
        
    monkeypatch.setattr(cli_module, "cmd_serve", mock_serve)
    cli_module.main()
    
    assert len(args_captured) == 1
    assert args_captured[0].port == 9000
    assert args_captured[0].host == "127.0.0.1"
    assert args_captured[0].bootstrap == "peer1:9000"

def test_cli_parse_query_name(monkeypatch):
    test_args = ["knarr", "query", "--bootstrap", "peer1:9000", "--name", "echo"]
    monkeypatch.setattr(sys, "argv", test_args)
    
    args_captured = []
    async def mock_query(args):
        args_captured.append(args)
        
    monkeypatch.setattr(cli_module, "cmd_query", mock_query)
    cli_module.main()
    
    assert args_captured[0].name == "echo"
    assert args_captured[0].tag is None

def test_cli_parse_query_tag(monkeypatch):
    test_args = ["knarr", "query", "--bootstrap", "peer1:9000", "--tag", "mcp"]
    monkeypatch.setattr(sys, "argv", test_args)
    
    args_captured = []
    async def mock_query(args):
        args_captured.append(args)
        
    monkeypatch.setattr(cli_module, "cmd_query", mock_query)
    cli_module.main()
    
    assert args_captured[0].tag == "mcp"
    assert args_captured[0].name is None

def test_cli_query_mutually_exclusive(monkeypatch):
    test_args = ["knarr", "query", "--bootstrap", "p", "--name", "n", "--tag", "t"]
    monkeypatch.setattr(sys, "argv", test_args)
    
    # Argparse handles this and calls sys.exit
    with pytest.raises(SystemExit):
        cli_module.main()

def test_cli_serve_defaults(monkeypatch):
    # In Phase 4, serve has defaults and port is optional
    test_args = ["knarr", "serve"]
    monkeypatch.setattr(sys, "argv", test_args)
    
    args_captured = []
    async def mock_serve(args):
        args_captured.append(args)
        
    monkeypatch.setattr(cli_module, "cmd_serve", mock_serve)
    cli_module.main()
    
    assert len(args_captured) == 1
    assert args_captured[0].port is None # None from CLI means use config/default
    assert args_captured[0].host is None
