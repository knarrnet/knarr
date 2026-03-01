import asyncio
import os
import pytest
import time
import tempfile
from knarr.cli.main import load_skills_from_config, cmd_serve
from unittest.mock import MagicMock, patch, AsyncMock

class MockNode:
    def __init__(self):
        self._handlers = {}
        self._handler_specs = {}
        self._handler_mtimes = {}
        self._skill_visibility = {}
        self._skill_allowed_nodes = {}
        self.register_handler = MagicMock()
        self.deregister = AsyncMock()
        self.announce = AsyncMock()
        self.node_info = MagicMock()
        self.node_info.node_id = "test-node"

@pytest.mark.asyncio
async def test_handler_reload_on_mtime_change():
    with tempfile.TemporaryDirectory() as tmpdir:
        handler_path = os.path.join(tmpdir, "handler.py")
        with open(handler_path, "w") as f:
            f.write('async def handle(data): return {"v": 1}')
        
        node = MockNode()
        config = {
            "skills": {
                "test": {"handler": "handler.py", "description": "test", "version": "1.0.0"}
            }
        }
        
        # Initial load
        await load_skills_from_config(node, config, tmpdir)
        node.register_handler.assert_called_once()
        assert "test" in node._handler_specs
        
        # Capture initial mtime
        initial_mtime = node._handler_mtimes["test"]
        
        # Modify handler - ensure mtime changes
        await asyncio.sleep(0.1)
        with open(handler_path, "w") as f:
            f.write('async def handle(data): return {"v": 2}')
        
        # Reset mocks
        node.register_handler.reset_mock()
        
        # Reload
        await load_skills_from_config(node, config, tmpdir)
        node.register_handler.assert_called_once()
        assert node._handler_mtimes["test"] > initial_mtime

@pytest.mark.asyncio
async def test_handler_reload_bad_syntax_keeps_old():
    with tempfile.TemporaryDirectory() as tmpdir:
        handler_path = os.path.join(tmpdir, "handler.py")
        with open(handler_path, "w") as f:
            f.write('async def handle(data): return {"v": 1}')
        
        node = MockNode()
        # Pre-populate node to simulate existing handler
        node._handlers["test"] = (MagicMock(), False)
        node._handler_specs["test"] = "handler.py"
        node._handler_mtimes["test"] = os.path.getmtime(handler_path)
        
        config = {
            "skills": {
                "test": {"handler": "handler.py", "description": "test"}
            }
        }
        
        # Change file to bad syntax
        await asyncio.sleep(0.1)
        with open(handler_path, "w") as f:
            f.write('async def handle(data): return {')
        
        # Reload
        await load_skills_from_config(node, config, tmpdir)
        
        # register_handler should NOT be called (re-registration failed)
        node.register_handler.assert_not_called()
        # Old handler remains in dict (it wasn't removed)
        assert "test" in node._handlers

@pytest.mark.asyncio
async def test_skill_removal_on_reload():
    with tempfile.TemporaryDirectory() as tmpdir:
        node = MockNode()
        # Simulate two registered skills
        node._handlers = {"s1": (MagicMock(), False), "s2": (MagicMock(), False)}
        node._handler_specs = {"s1": "h1.py", "s2": "h2.py"}
        
        config = {
            "skills": {
                "s1": {"handler": "h1.py", "description": "s1 desc"}
            }
        }
        
        await load_skills_from_config(node, config, tmpdir)
        
        # s2 should be deregistered and removed
        node.deregister.assert_awaited_once_with("s2")
        assert "s2" not in node._handlers
        assert "s2" not in node._handler_specs

@pytest.mark.asyncio
async def test_handler_specs_tracked():
    with tempfile.TemporaryDirectory() as tmpdir:
        handler_path = os.path.join(tmpdir, "handler.py")
        with open(handler_path, "w") as f:
            f.write('async def handle(data): return {}')
            
        node = MockNode()
        config = {
            "skills": {
                "test": {"handler": "handler.py", "description": "test"}
            }
        }
        
        await load_skills_from_config(node, config, tmpdir)
        assert node._handler_specs["test"] == "handler.py"
        assert node._handler_mtimes["test"] > 0

def test_pid_file_lifecycle():
    args = MagicMock()
    args.config = None
    args.port = 9000
    args.host = "127.0.0.1"
    args.storage = ":memory:"
    args.bootstrap = None
    args.advertise_host = "127.0.0.1"
    args.bridge = []
    args.cockpit = None
    args.log_level = "ERROR"

    with tempfile.TemporaryDirectory() as tmpdir:
        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            with open("knarr.toml", "w") as f:
                f.write("[node]\nport=9000")
            
            # Mock DHTNode and its start/stop
            with patch("knarr.cli.main.DHTNode") as MockDHTNode:
                mock_node_instance = MockDHTNode.return_value
                mock_node_instance.start = AsyncMock()
                mock_node_instance.stop = AsyncMock()
                mock_node_instance.join = AsyncMock(return_value=True)
                mock_node_instance.announce = AsyncMock()
                mock_node_instance.node_info.node_id = "test-node"
                mock_node_instance._restart_requested = False  # V011-009: prevent os.execv

                # Mock storage and enqueue_write
                mock_node_instance._enqueue_write = AsyncMock()
                mock_node_instance.register_system_skills = AsyncMock()

                # Patch asyncio.Event.wait to finish immediately
                with patch("asyncio.Event.wait", AsyncMock()):
                    asyncio.run(cmd_serve(args))
                    
                    # PID file should have been removed on exit
                    assert not os.path.exists("knarr.pid")
        finally:
            os.chdir(old_cwd)

@pytest.mark.asyncio
async def test_handler_reload_sentinel():
    with tempfile.TemporaryDirectory() as tmpdir:
        handler_path = os.path.join(tmpdir, "handler.py")
        with open(handler_path, "w") as f:
            f.write('async def handle(data): return {"result": "v1"}')
            
        node = MockNode()
        config = {"skills": {"test": {"handler": "handler.py", "description": "test"}}}
        
        await load_skills_from_config(node, config, tmpdir)
        node.register_handler.assert_called_once()
        
        # Capture the first handler function passed to register_handler
        first_handler = node.register_handler.call_args[0][1]
        
        await asyncio.sleep(0.1)
        with open(handler_path, "w") as f:
            f.write('async def handle(data): return {"result": "v2"}')
            
        node.register_handler.reset_mock()
        await load_skills_from_config(node, config, tmpdir)
        
        # register_handler should be called again with a NEW function object
        node.register_handler.assert_called_once()
        second_handler = node.register_handler.call_args[0][1]
        assert first_handler is not second_handler

@pytest.mark.asyncio
async def test_removal_sentinel():
    node = MockNode()
    node._handlers = {"test": (MagicMock(), False)}
    node._handler_specs = {"test": "h1.py"}
    
    await load_skills_from_config(node, {"skills": {}}, ".")
    node.deregister.assert_awaited_once_with("test")
    assert "test" not in node._handlers
