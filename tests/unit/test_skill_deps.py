import os
import pytest
import shutil
import tempfile
from knarr.cli.skill import cmd_skill_install, _extract_name_from_uri

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
            for uri, required in deps.items():
                req_str = "true" if required else "false"
                f.write(f'"{uri}" = {{ required = {req_str} }}\n')
                
    with open(os.path.join(path, "handler.py"), "w") as f:
        f.write("async def handle(data): return {}")

def test_dependency_warning_required_missing():
    with tempfile.TemporaryDirectory() as config_dir:
        with tempfile.TemporaryDirectory() as src_dir:
            create_dummy_skill(src_dir, "my-skill", deps={"knarr:///llm/chat@1.0": True})
            res = cmd_skill_install(src_dir, config_dir)
            assert "WARNING: Required dependency not found: knarr:///llm/chat@1.0" in res

def test_dependency_note_optional_missing():
    with tempfile.TemporaryDirectory() as config_dir:
        with tempfile.TemporaryDirectory() as src_dir:
            create_dummy_skill(src_dir, "my-skill", deps={"knarr:///tools/query@1.0": False})
            res = cmd_skill_install(src_dir, config_dir)
            assert "NOTE: Optional dependency not available: knarr:///tools/query@1.0" in res

def test_dependency_satisfied_no_warning():
    with tempfile.TemporaryDirectory() as config_dir:
        with tempfile.TemporaryDirectory() as tmp:
            # Install dep first
            dep_src = os.path.join(tmp, "chat")
            create_dummy_skill(dep_src, "chat")
            cmd_skill_install(dep_src, config_dir)
            
            # Install master
            master_src = os.path.join(tmp, "master")
            create_dummy_skill(master_src, "master", deps={"knarr:///llm/chat@1.0": True})
            res = cmd_skill_install(master_src, config_dir)
            assert "WARNING" not in res

def test_circular_dep_detection():
    with tempfile.TemporaryDirectory() as config_dir:
        with tempfile.TemporaryDirectory() as tmp:
            # Install A depending on B
            src_a = os.path.join(tmp, "a")
            create_dummy_skill(src_a, "a", deps={"knarr:///local/b": True})
            cmd_skill_install(src_a, config_dir)
            
            # Install B depending on A
            src_b = os.path.join(tmp, "b")
            create_dummy_skill(src_b, "b", deps={"knarr:///local/a": True})
            res = cmd_skill_install(src_b, config_dir)
            
            assert "Circular dependency detected" in res

def test_extract_name_from_uri():
    assert _extract_name_from_uri("knarr:///llm/chat@1.0") == "chat"
    assert _extract_name_from_uri("knarr:///tools/web/search@1.0") == "search"
    assert _extract_name_from_uri("simple-name") == "simple-name"
    assert _extract_name_from_uri("knarr:///name") == "name"
