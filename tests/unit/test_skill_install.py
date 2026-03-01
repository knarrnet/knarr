import os
import pytest
import shutil
import tempfile
import zipfile
import tomllib
from knarr.cli.skill import cmd_skill_install, cmd_skill_remove, cmd_skill_list

def create_dummy_skill(path, name="test-skill", version="0.1.0"):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "skill.toml"), "w") as f:
        f.write(f"""[skill]
name = "{name}"
version = "{version}"
handler = "handler.py:handle"
description = "test desc"
""")
    with open(os.path.join(path, "handler.py"), "w") as f:
        f.write("async def handle(data): return {}")

@pytest.fixture
def config_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir

def test_install_from_local_dir(config_dir):
    with tempfile.TemporaryDirectory() as src_dir:
        create_dummy_skill(src_dir, "my-skill")
        
        res = cmd_skill_install(src_dir, config_dir)
        assert "Installed my-skill" in res
        
        target = os.path.join(config_dir, "skills", "my-skill")
        assert os.path.exists(os.path.join(target, "skill.toml"))
        assert os.path.exists(os.path.join(target, "handler.py"))
        
        # Verify knarr.toml
        knarr_toml = os.path.join(config_dir, "knarr.toml")
        assert os.path.exists(knarr_toml)
        with open(knarr_toml, "rb") as f:
            cfg = tomllib.load(f)
        assert "my-skill" in cfg["skills"]
        assert cfg["skills"]["my-skill"]["version"] == "0.1.0"

def test_install_from_archive(config_dir):
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_src = os.path.join(tmpdir, "my-skill")
        create_dummy_skill(skill_src, "my-skill")
        
        archive_path = os.path.join(tmpdir, "my-skill.knarr")
        with zipfile.ZipFile(archive_path, "w") as zf:
            for root, dirs, files in os.walk(skill_src):
                for file in files:
                    zf.write(os.path.join(root, file), os.path.join("my-skill-0.1.0", file))
        
        res = cmd_skill_install(archive_path, config_dir)
        assert "Installed my-skill" in res
        assert os.path.exists(os.path.join(config_dir, "skills", "my-skill", "skill.toml"))

def test_install_preserves_data_on_upgrade(config_dir):
    with tempfile.TemporaryDirectory() as src_dir:
        create_dummy_skill(src_dir, "my-skill", "0.1.0")
        cmd_skill_install(src_dir, config_dir)
        
        # Create some data
        data_file = os.path.join(config_dir, "skills", "my-skill", "data", "test.txt")
        os.makedirs(os.path.dirname(data_file), exist_ok=True)
        with open(data_file, "w") as f: f.write("keep me")
        
        # Upgrade
        create_dummy_skill(src_dir, "my-skill", "0.2.0")
        cmd_skill_install(src_dir, config_dir, upgrade=True)
        
        assert os.path.exists(data_file)
        with open(data_file, "r") as f: assert f.read() == "keep me"

def test_install_metadata_written(config_dir):
    with tempfile.TemporaryDirectory() as src_dir:
        create_dummy_skill(src_dir, "my-skill")
        cmd_skill_install(src_dir, config_dir)
        
        installed_toml = os.path.join(config_dir, "skills", "my-skill", "skill.toml")
        with open(installed_toml, "rb") as f:
            manifest = tomllib.load(f)
        assert "install" in manifest
        assert manifest["install"]["source"] == src_dir
        assert "installed_at" in manifest["install"]

def test_remove_preserves_data(config_dir):
    with tempfile.TemporaryDirectory() as src_dir:
        create_dummy_skill(src_dir, "my-skill")
        cmd_skill_install(src_dir, config_dir)
        
        data_file = os.path.join(config_dir, "skills", "my-skill", "data", "test.txt")
        os.makedirs(os.path.dirname(data_file), exist_ok=True)
        with open(data_file, "w") as f: f.write("save")
        
        cmd_skill_remove("my-skill", config_dir)
        
        # Handler gone, data stays
        assert not os.path.exists(os.path.join(config_dir, "skills", "my-skill", "handler.py"))
        assert os.path.exists(data_file)

def test_remove_purge_deletes_everything(config_dir):
    with tempfile.TemporaryDirectory() as src_dir:
        create_dummy_skill(src_dir, "my-skill")
        cmd_skill_install(src_dir, config_dir)
        
        cmd_skill_remove("my-skill", config_dir, purge=True)
        assert not os.path.exists(os.path.join(config_dir, "skills", "my-skill"))

def test_skill_list_format(config_dir):
    with tempfile.TemporaryDirectory() as src_dir:
        create_dummy_skill(src_dir, "s1")
        cmd_skill_install(src_dir, config_dir)
        
        res = cmd_skill_list(config_dir)
        assert "s1" in res
        assert "0.1.0" in res

def test_install_conflict_same_version(config_dir):
    with tempfile.TemporaryDirectory() as src_dir:
        create_dummy_skill(src_dir, "my-skill", "1.0.0")
        cmd_skill_install(src_dir, config_dir)
        
        # Install again
        res = cmd_skill_install(src_dir, config_dir)
        assert "already installed" in res
        assert "use --force to overwrite" in res

def test_knarr_toml_updated(config_dir):
    with tempfile.TemporaryDirectory() as src_dir:
        toml_path = os.path.join(src_dir, "skill.toml")
        os.makedirs(src_dir, exist_ok=True)
        with open(toml_path, "w") as f:
            f.write("""[skill]
name = "rich-skill"
version = "1.0.0"
handler = "h.py:run"
description = "rich"

[skill.schema]
input = { a = "int" }

[skill.pricing]
price = 5.0

[skill.visibility]
default = "whitelist"
""")
        with open(os.path.join(src_dir, "h.py"), "w") as f: f.write("async def run(d): return {}")
        
        cmd_skill_install(src_dir, config_dir)
        
        with open(os.path.join(config_dir, "knarr.toml"), "rb") as f:
            cfg = tomllib.load(f)
        
        skill = cfg["skills"]["rich-skill"]
        assert skill["price"] == 5.0
        assert skill["visibility"] == "whitelist"
        assert skill["input_schema"]["a"] == "int"
        assert skill["handler"] == "skills/rich-skill/h.py:run"
