"""Tests for version awareness: status fields, upgrade module, RETRY_AFTER."""
import pytest
from unittest.mock import patch, MagicMock
from knarr.dht.upgrade import check_and_upgrade


class TestUpgradeVerification:
    """SHA256 verification in auto-upgrade module."""

    @patch("knarr.dht.upgrade.urllib.request.urlopen")
    def test_sha256_mismatch_aborts(self, mock_urlopen):
        """Non-negotiable: SHA256 mismatch must abort."""
        import io
        import hashlib

        # Fake SHA256SUMS with a hash that won't match
        fake_sums = "deadbeef" * 8 + "  knarr-99.0.0.tar.gz\n"
        fake_tarball = b"fake tarball data"

        # First call: SHA256SUMS, second call: tarball
        mock_resp_sums = MagicMock()
        mock_resp_sums.read.return_value = fake_sums.encode()
        mock_resp_sums.__enter__ = lambda s: s
        mock_resp_sums.__exit__ = MagicMock(return_value=False)

        mock_resp_tar = MagicMock()
        mock_resp_tar.read.return_value = fake_tarball
        mock_resp_tar.__enter__ = lambda s: s
        mock_resp_tar.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [mock_resp_sums, mock_resp_tar]

        result = check_and_upgrade("99.0.0")
        assert result is False  # Must abort on mismatch

    @patch("knarr.dht.upgrade.urllib.request.urlopen")
    def test_missing_sdist_in_sums(self, mock_urlopen):
        """If sdist not in SHA256SUMS, abort."""
        fake_sums = "abc123  some-other-file.tar.gz\n"
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_sums.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = check_and_upgrade("99.0.0")
        assert result is False

    @patch("knarr.dht.upgrade.urllib.request.urlopen")
    def test_network_error_returns_false(self, mock_urlopen):
        """Network errors should return False, not crash."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        result = check_and_upgrade("99.0.0")
        assert result is False


class TestRetryAfterDuringUpgrade:
    """RETRY_AFTER must be returned when node is upgrading."""

    def test_upgrading_flag_in_status(self):
        """get_status should reflect _upgrading flag."""
        from knarr.dht.node import DHTNode
        node = DHTNode("127.0.0.1", 0, config={"node": {}})
        node._start_time = 0
        assert node._upgrading is False
        assert node.get_status()["upgrading"] is False
        node._upgrading = True
        assert node.get_status()["upgrading"] is True

    def test_auto_upgrade_off_by_default(self):
        """auto_upgrade must be opt-in (off by default)."""
        from knarr.dht.node import DHTNode
        node = DHTNode("127.0.0.1", 0, config={"node": {}})
        node._start_time = 0
        assert node.get_status()["auto_upgrade"] is False
