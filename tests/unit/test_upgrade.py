import os
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch, MagicMock
import pytest
from knarr.dht.upgrade import get_latest_version, backup_config, rollback_installation, verify_installation

def test_get_latest_version_parses_github_response():
    """get_latest_version parses tag_name from GitHub API response."""
    with patch("urllib.request.urlopen") as mock_url:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"tag_name": "v0.14.0"}'
        mock_resp.__enter__.return_value = mock_resp
        mock_url.return_value = mock_resp
        
        assert get_latest_version() == "0.14.0"

def test_backup_creates_directory_and_copies_files():  # SENTINEL
    """backup_config creates backup dir and copies identity files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create dummy files
        config_file = os.path.join(tmpdir, "knarr.toml")
        with open(config_file, "w") as f: f.write("config")
        seed_file = os.path.join(tmpdir, "node_key.seed")
        with open(seed_file, "w") as f: f.write("seed")
        
        backup_path = backup_config(tmpdir, "0.13.0")
        assert backup_path is not None
        assert os.path.exists(backup_path)
        assert "backup-v0.13.0" in backup_path
        assert os.path.exists(os.path.join(backup_path, "knarr.toml"))
        assert os.path.exists(os.path.join(backup_path, "node_key.seed"))

def test_rollback_restores_from_backup():
    """rollback_installation copies files back from backup."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = os.path.join(tmpdir, "config")
        backup_dir = os.path.join(tmpdir, "backup")
        os.makedirs(config_dir)
        os.makedirs(backup_dir)
        
        # Original file
        config_file = os.path.join(config_dir, "knarr.toml")
        with open(config_file, "w") as f: f.write("original")
        
        # Backup file
        with open(os.path.join(backup_dir, "knarr.toml"), "w") as f: f.write("backed_up")
        
        # Rollback
        assert rollback_installation(backup_dir, config_dir) is True
        
        with open(config_file, "r") as f:
            assert f.read() == "backed_up"

def test_verify_installation_checks_version():
    """verify_installation returns True when version matches."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="0.14.0\n")
        assert verify_installation("0.14.0") is True
        
        mock_run.return_value = MagicMock(returncode=0, stdout="0.13.0\n")
        assert verify_installation("0.14.0") is False
