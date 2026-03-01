"""Tests for read-only Solana RPC balance queries."""
import json
import pytest
from unittest.mock import patch, MagicMock

from knarr.core.solana_rpc import (
    get_token_balance, get_sol_balance, _validate_rpc_url, _parse_token_amount,
)


def _mock_urlopen(response_data):
    """Create a mock for urllib.request.urlopen that returns JSON response."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(response_data).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestTokenBalance:
    @pytest.mark.asyncio
    async def test_parses_response(self):
        """Mock RPC response parsed correctly."""
        rpc_response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {"value": [{
                "account": {"data": {"parsed": {"info": {
                    "tokenAmount": {"uiAmount": 1250.5}
                }}}}
            }]}
        }
        with patch("knarr.core.solana_rpc.urllib.request.urlopen",
                    return_value=_mock_urlopen(rpc_response)):
            result = await get_token_balance("FakeWallet", "FakeMint")
        assert result == 1250.5

    @pytest.mark.asyncio
    async def test_zero_when_no_account(self):
        """Empty accounts array returns 0.0."""
        rpc_response = {"jsonrpc": "2.0", "id": 1, "result": {"value": []}}
        with patch("knarr.core.solana_rpc.urllib.request.urlopen",
                    return_value=_mock_urlopen(rpc_response)):
            result = await get_token_balance("FakeWallet", "FakeMint")
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_failure_returns_none(self):
        """URLError returns None, no crash."""
        with patch("knarr.core.solana_rpc.urllib.request.urlopen",
                    side_effect=Exception("Connection refused")):
            result = await get_token_balance("FakeWallet", "FakeMint")
        assert result is None

    @pytest.mark.asyncio
    async def test_multi_account_sums(self):
        """V012a-001: Multiple token accounts are summed."""
        rpc_response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {"value": [
                {"account": {"data": {"parsed": {"info": {
                    "tokenAmount": {"uiAmount": 1.5}
                }}}}},
                {"account": {"data": {"parsed": {"info": {
                    "tokenAmount": {"uiAmount": 2.0}
                }}}}},
            ]}
        }
        with patch("knarr.core.solana_rpc.urllib.request.urlopen",
                    return_value=_mock_urlopen(rpc_response)):
            result = await get_token_balance("FakeWallet", "FakeMint")
        assert result == 3.5

    @pytest.mark.asyncio
    async def test_rpc_error_returns_none(self):
        """V012a-002: JSON-RPC error payload returns None, not 0.0."""
        rpc_response = {
            "jsonrpc": "2.0", "id": 1,
            "error": {"code": -32600, "message": "Invalid request"}
        }
        with patch("knarr.core.solana_rpc.urllib.request.urlopen",
                    return_value=_mock_urlopen(rpc_response)):
            result = await get_token_balance("FakeWallet", "FakeMint")
        assert result is None


class TestParseTokenAmount:
    def test_ui_amount_numeric(self):
        account = {"account": {"data": {"parsed": {"info": {
            "tokenAmount": {"uiAmount": 42.5}
        }}}}}
        assert _parse_token_amount(account) == 42.5

    def test_ui_amount_none_falls_back_to_string(self):
        """V012a-001: uiAmount=None uses uiAmountString."""
        account = {"account": {"data": {"parsed": {"info": {
            "tokenAmount": {"uiAmount": None, "uiAmountString": "100.25"}
        }}}}}
        assert _parse_token_amount(account) == 100.25

    def test_raw_amount_decimals_fallback(self):
        """V012a-001: Falls back to amount/decimals when both ui fields absent."""
        account = {"account": {"data": {"parsed": {"info": {
            "tokenAmount": {"amount": "5000000", "decimals": 6}
        }}}}}
        assert _parse_token_amount(account) == 5.0

    def test_malformed_returns_zero(self):
        assert _parse_token_amount({"account": {}}) == 0.0


class TestSolBalance:
    @pytest.mark.asyncio
    async def test_parses_lamports(self):
        """1_000_000_000 lamports = 1.0 SOL."""
        rpc_response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {"value": 1_000_000_000}
        }
        with patch("knarr.core.solana_rpc.urllib.request.urlopen",
                    return_value=_mock_urlopen(rpc_response)):
            result = await get_sol_balance("FakeWallet")
        assert result == 1.0

    @pytest.mark.asyncio
    async def test_failure_returns_none(self):
        """Network error returns None."""
        with patch("knarr.core.solana_rpc.urllib.request.urlopen",
                    side_effect=Exception("timeout")):
            result = await get_sol_balance("FakeWallet")
        assert result is None

    @pytest.mark.asyncio
    async def test_rpc_error_returns_none(self):
        """V012a-002: JSON-RPC error payload returns None, not 0.0."""
        rpc_response = {
            "jsonrpc": "2.0", "id": 1,
            "error": {"code": -32005, "message": "Rate limited"}
        }
        with patch("knarr.core.solana_rpc.urllib.request.urlopen",
                    return_value=_mock_urlopen(rpc_response)):
            result = await get_sol_balance("FakeWallet")
        assert result is None


class TestRpcUrlValidation:
    @pytest.mark.asyncio
    async def test_custom_url_used(self):
        """Custom RPC URL is passed through to urllib."""
        custom_url = "https://my-custom-rpc.example.com"
        captured_urls = []

        def mock_urlopen(req, timeout=None):
            captured_urls.append(req.full_url)
            return _mock_urlopen({"jsonrpc": "2.0", "id": 1, "result": {"value": 0}})

        with patch("knarr.core.solana_rpc.urllib.request.urlopen", side_effect=mock_urlopen):
            await get_sol_balance("FakeWallet", rpc_url=custom_url)
        assert captured_urls[0] == custom_url

    def test_rejects_file_scheme(self):
        """V012a-003: file:// scheme rejected."""
        with pytest.raises(ValueError, match="scheme must be https or http"):
            _validate_rpc_url("file:///etc/passwd")

    def test_rejects_ftp_scheme(self):
        """V012a-003: ftp:// scheme rejected."""
        with pytest.raises(ValueError, match="scheme must be https or http"):
            _validate_rpc_url("ftp://evil.com/data")

    def test_accepts_https(self):
        assert _validate_rpc_url("https://api.mainnet-beta.solana.com") == "https://api.mainnet-beta.solana.com"

    def test_accepts_http(self):
        assert _validate_rpc_url("http://localhost:8899") == "http://localhost:8899"
