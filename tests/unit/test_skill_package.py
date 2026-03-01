import os
import pytest
import shutil
import tempfile
from knarr.cli.config import parse_skill_toml
from knarr.cli.skill import cmd_skill_init

def test_parse_skill_toml_valid():
    with tempfile.TemporaryDirectory() as tmpdir:
        toml_path = os.path.join(tmpdir, "skill.toml")
        content = """
[skill]
name = "test-skill"
version = "1.0.0"
handler = "handler.py:handle"
description = "A test skill"
tags = ["test", "demo"]

[skill.schema]
input = { text = "string" }
output = { result = "string" }
"""
        with open(toml_path, "w") as f:
            f.write(content)
        
        manifest = parse_skill_toml(toml_path)
        assert manifest["name"] == "test-skill"
        assert manifest["version"] == "1.0.0"
        assert manifest["handler"] == "handler.py:handle"
        assert manifest["description"] == "A test skill"
        assert manifest["tags"] == ["test", "demo"]
        assert manifest["schema"]["input"]["text"] == "string"

def test_parse_skill_toml_missing_name():
    with tempfile.TemporaryDirectory() as tmpdir:
        toml_path = os.path.join(tmpdir, "skill.toml")
        content = """
[skill]
version = "1.0.0"
handler = "handler.py:handle"
"""
        with open(toml_path, "w") as f:
            f.write(content)
        
        with pytest.raises(ValueError, match=r"\[skill\].name is required"):
            parse_skill_toml(toml_path)

def test_parse_skill_toml_missing_version():
    with tempfile.TemporaryDirectory() as tmpdir:
        toml_path = os.path.join(tmpdir, "skill.toml")
        content = """
[skill]
name = "test-skill"
handler = "handler.py:handle"
"""
        with open(toml_path, "w") as f:
            f.write(content)
        
        with pytest.raises(ValueError, match=r"\[skill\].version is required"):
            parse_skill_toml(toml_path)

def test_parse_skill_toml_missing_handler():
    with tempfile.TemporaryDirectory() as tmpdir:
        toml_path = os.path.join(tmpdir, "skill.toml")
        content = """
[skill]
name = "test-skill"
version = "1.0.0"
"""
        with open(toml_path, "w") as f:
            f.write(content)
        
        with pytest.raises(ValueError, match=r"\[skill\].handler is required"):
            parse_skill_toml(toml_path)

def test_parse_skill_toml_invalid_name():
    with tempfile.TemporaryDirectory() as tmpdir:
        toml_path = os.path.join(tmpdir, "skill.toml")
        
        # Uppercase
        with open(toml_path, "w") as f:
            f.write("""[skill]
name = "Test-Skill"
version = "1.0.0"
handler = "h.py" """)
        with pytest.raises(ValueError, match="name must be lowercase"):
            parse_skill_toml(toml_path)
            
        # Spaces
        with open(toml_path, "w") as f:
            f.write("""[skill]
name = "test skill"
version = "1.0.0"
handler = "h.py" """)
        with pytest.raises(ValueError, match="name must be lowercase"):
            parse_skill_toml(toml_path)

        # Long name
        with open(toml_path, "w") as f:
            f.write(f"""[skill]
name = "{"a"*65}"
version = "1.0.0"
handler = "h.py" """)
        with pytest.raises(ValueError, match="max 64 chars"):
            parse_skill_toml(toml_path)

def test_parse_skill_toml_asset_tiers():
    with tempfile.TemporaryDirectory() as tmpdir:
        toml_path = os.path.join(tmpdir, "skill.toml")
        content = """
[skill]
name = "test-skill"
version = "1.0.0"
handler = "h.py"

[skill.assets.hot]
db = { path = "assets/db" }

[skill.assets.fetch]
model = { url = "http://example.com", hash = "sha256:abc" }
"""
        with open(toml_path, "w") as f:
            f.write(content)
        
        manifest = parse_skill_toml(toml_path)
        assert "db" in manifest["assets"]["hot"]
        assert "model" in manifest["assets"]["fetch"]

def test_skill_init_creates_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            cmd_skill_init("my-skill")
            assert os.path.isdir("my-skill")
            assert os.path.exists("my-skill/skill.toml")
            assert os.path.exists("my-skill/handler.py")
            
            with open("my-skill/skill.toml", "r") as f:
                content = f.read()
                assert 'name = "my-skill"' in content
                assert 'version = "0.1.0"' in content
        finally:
            os.chdir(old_cwd)

def test_skill_init_handler_is_valid_python():
    with tempfile.TemporaryDirectory() as tmpdir:
        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            cmd_skill_init("my-skill")
            with open("my-skill/handler.py", "r") as f:
                code = f.read()
            
            # Use compile to check for syntax errors
            compile(code, "handler.py", "exec")
            
            # Check for async def handle
            assert "async def handle" in code
        finally:
            os.chdir(old_cwd)
