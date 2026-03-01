import os
import pytest
import shutil
import tempfile
import zipfile
import tomllib
import sys
from knarr.cli.skill import cmd_skill_install, _write_knarr_toml, cmd_skill_remove
from knarr.cli.config import parse_skill_toml, load_handler
from knarr.cli.main import main, load_skills_from_config
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

def test_zip_slip_prevention():
    with tempfile.TemporaryDirectory() as tmpdir:
        archive_path = os.path.join(tmpdir, "evil.knarr")
        with zipfile.ZipFile(archive_path, "w") as zf:
            # Entry with .. components
            zf.writestr("myskill-1.0.0/../../evil.txt", "evil")
        
        with pytest.raises(ValueError, match="malicious entry name detected"):
            cmd_skill_install(archive_path, tmpdir)

def test_cli_wiring_no_config_list():
    # Verify 'knarr skill list' doesn't crash when --config is omitted
    # We mock cmd_skill_list to avoid dependency on actual files
    with patch("knarr.cli.skill.cmd_skill_list", return_value="ok") as mock_list:
        with patch("sys.argv", ["knarr", "skill", "list"]):
            with patch("sys.stdout", new=MagicMock()):
                main()
                mock_list.assert_called_once()

def test_toml_roundtrip_preservation():
    with tempfile.TemporaryDirectory() as tmpdir:
        toml_path = os.path.join(tmpdir, "knarr.toml")
        content = """[node]
port = 9000

[sidecar]
asset_dir = "myassets"
max_total_size = 1000

[skills.echo]
handler = "h.py"
"""
        with open(toml_path, "w") as f:
            f.write(content)
            
        with open(toml_path, "rb") as f:
            config = tomllib.load(f)
            
        # Write it back
        _write_knarr_toml(toml_path, config)
        
        # Read back and verify sidecar section exists
        with open(toml_path, "rb") as f:
            new_config = tomllib.load(f)
            
        assert "sidecar" in new_config
        assert new_config["sidecar"]["asset_dir"] == "myassets"
        assert new_config["sidecar"]["max_total_size"] == 1000
        assert "echo" in new_config["skills"]

@pytest.mark.asyncio
async def test_module_cleanup_verification():
    with tempfile.TemporaryDirectory() as tmpdir:
        handler_path = os.path.join(tmpdir, "handler.py")
        with open(handler_path, "w") as f:
            f.write('async def handle(data): return {}')
            
        node = MockNode()
        # Mock register_handler to actually define the module in sys.modules (simulating load_handler)
        # Actually, let's use load_handler directly to test the integration
        
        config = {
            "skills": {
                "cleanup-test": {"handler": "handler.py", "description": "test"}
            }
        }
        
        # 1. Load skill
        await load_skills_from_config(node, config, tmpdir)
        module_name = "knarr_skill_cleanup-test"
        assert module_name in sys.modules
        
        # 2. Remove from config and reload
        await load_skills_from_config(node, {"skills": {}}, tmpdir)
        
        # 3. Verify module removed from sys.modules
        assert module_name not in sys.modules
