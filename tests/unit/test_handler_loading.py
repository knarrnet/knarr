import os
import pytest
from knarr.cli.config import load_handler
import asyncio

def test_load_handler_success(tmp_path):
    skill_file = tmp_path / "skill.py"
    skill_file.write_text("""
async def my_handle(data):
    return {"ok": True}
""")
    
    handler = load_handler(str(skill_file) + ":my_handle", str(tmp_path))
    assert callable(handler)
    
    res = asyncio.run(handler({}))
    assert res == {"ok": True}

def test_load_handler_default_name(tmp_path):
    skill_file = tmp_path / "skill.py"
    skill_file.write_text("""
async def handle(data):
    return 1
""")
    handler = load_handler("skill.py", str(tmp_path))
    assert asyncio.run(handler({})) == 1

def test_load_handler_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_handler("no.py", str(tmp_path))

def test_load_handler_missing_func(tmp_path):
    skill_file = tmp_path / "skill.py"
    skill_file.write_text("async def handle(d): pass")
    with pytest.raises(ImportError, match="No function 'wrong' found"):
        load_handler(str(skill_file) + ":wrong", str(tmp_path))