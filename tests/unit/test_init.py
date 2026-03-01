import os
import shutil
from pathlib import Path
from knarr.cli.init import init_project

def test_init_project(tmp_path):
    target = tmp_path / "test-project"
    summary = init_project(str(target), port=8888, bootstrap="boot:99")
    
    assert target.exists()
    assert (target / "knarr.toml").exists()
    assert (target / "skills" / "echo.py").exists()
    
    config_text = (target / "knarr.toml").read_text()
    assert "port = 8888" in config_text
    assert "bootstrap = [\"boot:99\"]" in config_text
    
    assert "Created project" in summary

def test_init_project_non_empty(tmp_path):
    target = tmp_path / "busy"
    target.mkdir()
    (target / "file.txt").touch()
    
    import pytest
    with pytest.raises(SystemExit):
        init_project(str(target))