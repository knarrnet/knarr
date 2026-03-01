import os
import pytest
import shutil
import tempfile
import zipfile
import tomllib
from knarr.cli.skill import cmd_skill_pack, cmd_skill_export, cmd_skill_install

def create_dummy_skill(path, name="test-skill", version="0.1.0", deps=None):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "skill.toml"), "w") as f:
        f.write(f"""[skill]
name = "{name}"
version = "{version}"
handler = "handler.py:handle"
description = "desc"
""")
        if deps:
            f.write("\n[dependencies]\n")
            for dep in deps:
                # Ensure URIs with special characters are correctly handled
                f.write(f'"{dep}" = {{ required = true }}\n')
                
    with open(os.path.join(path, "handler.py"), "w") as f:
        f.write("async def handle(data): return {}")

def test_pack_creates_valid_zip():
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = os.path.join(tmpdir, "my-skill")
        create_dummy_skill(skill_dir, "my-skill", "1.2.3")
        
        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            res = cmd_skill_pack("my-skill")
            assert "Created my-skill-1.2.3.knarr" in res
            
            archive = "my-skill-1.2.3.knarr"
            assert os.path.exists(archive)
            
            with zipfile.ZipFile(archive, "r") as zf:
                names = zf.namelist()
                assert "my-skill-1.2.3/skill.toml" in names
                assert "my-skill-1.2.3/handler.py" in names
        finally:
            os.chdir(old_cwd)

def test_pack_excludes_tests_and_pycache():
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = os.path.join(tmpdir, "my-skill")
        create_dummy_skill(skill_dir, "my-skill")
        os.makedirs(os.path.join(skill_dir, "tests"))
        os.makedirs(os.path.join(skill_dir, "__pycache__"))
        with open(os.path.join(skill_dir, "tests", "test_h.py"), "w") as f: f.write("pass")
        
        archive_name = "test.knarr"
        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            cmd_skill_pack("my-skill")
            archive = "my-skill-0.1.0.knarr"
            with zipfile.ZipFile(archive, "r") as zf:
                names = zf.namelist()
                for n in names:
                    assert "tests/" not in n
                    assert "__pycache__/" not in n
        finally:
            os.chdir(old_cwd)

def test_export_installed_skill():
    with tempfile.TemporaryDirectory() as config_dir:
        with tempfile.TemporaryDirectory() as src_dir:
            create_dummy_skill(src_dir, "my-skill", "2.0.0")
            cmd_skill_install(src_dir, config_dir)
            
            old_cwd = os.getcwd()
            os.chdir(config_dir)
            try:
                res = cmd_skill_export("my-skill", config_dir)
                assert "Created my-skill-2.0.0.knarr" in res
                assert os.path.exists("my-skill-2.0.0.knarr")
            finally:
                os.chdir(old_cwd)

def test_export_bundle_includes_deps():
    with tempfile.TemporaryDirectory() as config_dir:
        with tempfile.TemporaryDirectory() as tmp:
            # Create and install dep
            dep_src = os.path.join(tmp, "dep-skill")
            create_dummy_skill(dep_src, "dep-skill", "1.0.0")
            cmd_skill_install(dep_src, config_dir)
            
            # Create and install master
            master_src = os.path.join(tmp, "master-skill")
            create_dummy_skill(master_src, "master-skill", "1.0.0", deps=["knarr:///shared/dep-skill@1.0"])
            cmd_skill_install(master_src, config_dir)
            
            old_cwd = os.getcwd()
            os.chdir(config_dir)
            try:
                res = cmd_skill_export("master-skill", config_dir, bundle=True)
                assert "Bundled: dep-skill-1.0.0" in res
                
                with zipfile.ZipFile("master-skill-1.0.0.knarr", "r") as zf:
                    names = zf.namelist()
                    assert "master-skill-1.0.0/deps/dep-skill-1.0.0.knarr" in names
            finally:
                os.chdir(old_cwd)

def test_archive_root_directory():
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = os.path.join(tmpdir, "my-skill")
        create_dummy_skill(skill_dir, "my-skill", "1.0.0")
        
        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            cmd_skill_pack("my-skill")
            with zipfile.ZipFile("my-skill-1.0.0.knarr", "r") as zf:
                for name in zf.namelist():
                    assert name.startswith("my-skill-1.0.0/")
        finally:
            os.chdir(old_cwd)
