"""Tests for Track A2: BCW Plugin Chain ID Fix (v0.40.0).

Tests that derive_counterparty_address() and _derive_master_address() now
accept any chain_id starting with "solana" (not just "solana-mainnet"),
enabling testnet and devnet usage.
"""
import sys
import os
import hashlib
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))


# We import only the free functions, not the plugin class (which requires
# nacl and a vault). nacl is a real dep so we need to handle it.
try:
    from nacl.signing import SigningKey
    _HAS_NACL = True
except ImportError:
    _HAS_NACL = False

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# 64-char hex node_id stub
_NODE_ID = "ab" * 32  # 64 hex chars
_MASTER_SEED = bytes(range(32))  # 32-byte seed


def _load_handler_module():
    """Import the BCW handler module."""
    # Need to stub out the solana sibling import
    mock_solana = MagicMock()
    mock_solana.SolanaWatcher = MagicMock()
    mock_solana.PollResult = MagicMock()

    with patch.dict(sys.modules, {
        "solana": mock_solana,
        "knarr.plugins.10-bcw.solana": mock_solana,
    }):
        # The handler imports nacl, so skip if unavailable
        import importlib
        spec = importlib.util.spec_from_file_location(
            "bcw_handler",
            os.path.join(
                os.path.dirname(__file__),
                "../../src/knarr/plugins/10-bcw/handler.py"
            )
        )
        mod = importlib.util.module_from_spec(spec)
        # Patch solana in sys.modules before exec
        sys.modules["solana"] = mock_solana
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.modules.pop("solana", None)
        return mod


# ---------------------------------------------------------------------------
# Source-level check (does not require nacl import to succeed)
# ---------------------------------------------------------------------------

def test_source_has_startswith_solana():
    """Verify the handler.py source was patched to use startswith('solana-')."""
    handler_path = os.path.join(
        os.path.dirname(__file__),
        "../../src/knarr/plugins/10-bcw/handler.py"
    )
    with open(handler_path, "r", encoding="utf-8") as f:
        source = f.read()

    # All checks should use startswith("solana-") (with dash, not bare "solana")
    assert 'startswith("solana-")' in source, (
        'Expected startswith("solana-") in handler.py'
    )
    # Should not have any remaining == "solana-mainnet" chains
    import re
    eq_mainnet = re.findall(r'chain_id\s*==\s*["\']solana-mainnet["\']', source)
    assert not eq_mainnet, (
        f"Found {len(eq_mainnet)} remaining == 'solana-mainnet' check(s): {eq_mainnet}"
    )


# ---------------------------------------------------------------------------
# Runtime tests — require nacl
# ---------------------------------------------------------------------------

pytestmark_nacl = pytest.mark.skipif(not _HAS_NACL, reason="nacl not installed")


@pytestmark_nacl
def test_mainnet_accepted():
    """solana-mainnet is still accepted after the fix."""
    mod = _load_handler_module()
    # Should not raise
    addr = mod.derive_counterparty_address(_MASTER_SEED, _NODE_ID, "solana-mainnet")
    assert isinstance(addr, str)
    assert len(addr) > 0


@pytestmark_nacl
def test_testnet_accepted():
    """solana-testnet is now accepted."""
    mod = _load_handler_module()
    addr = mod.derive_counterparty_address(_MASTER_SEED, _NODE_ID, "solana-testnet")
    assert isinstance(addr, str)
    assert len(addr) > 0


@pytestmark_nacl
def test_devnet_accepted():
    """solana-devnet is now accepted."""
    mod = _load_handler_module()
    addr = mod.derive_counterparty_address(_MASTER_SEED, _NODE_ID, "solana-devnet")
    assert isinstance(addr, str)
    assert len(addr) > 0


@pytestmark_nacl
def test_non_solana_rejected():
    """Non-solana chains are still rejected."""
    mod = _load_handler_module()
    with pytest.raises(ValueError, match="Unsupported chain"):
        mod.derive_counterparty_address(_MASTER_SEED, _NODE_ID, "ethereum-mainnet")


@pytestmark_nacl
def test_derive_master_address_testnet():
    """_derive_master_address also accepts solana-testnet."""
    mod = _load_handler_module()
    addr = mod._derive_master_address(_MASTER_SEED, "solana-testnet")
    assert isinstance(addr, str)
    assert len(addr) > 0


@pytestmark_nacl
def test_derive_master_address_non_solana_rejected():
    """_derive_master_address rejects non-solana chains."""
    mod = _load_handler_module()
    with pytest.raises(ValueError, match="Unsupported chain"):
        mod._derive_master_address(_MASTER_SEED, "bitcoin-mainnet")


# ---------------------------------------------------------------------------
# Source-level check for SolanaWatcher instantiation (third occurrence)
# ---------------------------------------------------------------------------

def test_solana_watcher_init_uses_startswith():
    """The SolanaWatcher init block uses startswith too."""
    handler_path = os.path.join(
        os.path.dirname(__file__),
        "../../src/knarr/plugins/10-bcw/handler.py"
    )
    with open(handler_path, "r", encoding="utf-8") as f:
        source = f.read()

    # Count all startswith("solana-") occurrences (tightened from bare "solana")
    import re
    count = len(re.findall(r'startswith\(["\']solana-["\']\)', source))
    # Expect at least 3 (derive_counterparty, _derive_master, SolanaWatcher init)
    assert count >= 3, f"Expected >= 3 startswith('solana-') checks, found {count}"
