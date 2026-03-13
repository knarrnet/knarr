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
    """backup_config is a no-op stub — returns None and emits DeprecationWarning (v0.45.0)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.warns(DeprecationWarning, match="knarr-watchman"):
            result = backup_config(tmpdir, "0.13.0")
        assert result is None, "backup_config must return None (deprecated no-op)"

def test_rollback_restores_from_backup():
    """rollback_installation is a no-op stub — returns False and emits DeprecationWarning (v0.45.0)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = os.path.join(tmpdir, "config")
        backup_dir = os.path.join(tmpdir, "backup")
        os.makedirs(config_dir)
        os.makedirs(backup_dir)
        with pytest.warns(DeprecationWarning, match="knarr-watchman"):
            result = rollback_installation(backup_dir, config_dir)
        assert result is False, "rollback_installation must return False (deprecated no-op)"

def test_verify_installation_checks_version():
    """verify_installation is a no-op stub — returns False and emits DeprecationWarning (v0.45.0)."""
    with pytest.warns(DeprecationWarning, match="knarr-watchman"):
        result = verify_installation("0.14.0")
    assert result is False, "verify_installation must return False (deprecated no-op)"
