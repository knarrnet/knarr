"""
Chain selector — A6.

get_chain_config() reads [blockchain] section from config and returns
a unified chain description dict. All callers (BCW, wallet plugin, node)
go through this single function so chain selection is configured in one place.

Known chains:
    solana-devnet    — default development chain
    solana-testnet   — Solana public testnet
    solana-mainnet   — Solana mainnet-beta (production)

Config layout:

    [blockchain]
    chain = "solana-devnet"

    [blockchain.networks.solana-devnet]
    rpc_url    = "https://api.devnet.solana.com"
    commitment = "confirmed"
    token_mint = ""          # empty on devnet — no real mint

    [blockchain.networks.solana-mainnet]
    rpc_url    = "https://api.mainnet-beta.solana.com"
    commitment = "finalized"
    token_mint = "KNARRxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# ── Built-in network defaults ──────────────────────────────────────────
_DEFAULTS: Dict[str, Dict[str, str]] = {
    "solana-devnet": {
        "rpc_url": "https://api.devnet.solana.com",
        "commitment": "confirmed",
        "token_mint": "",
    },
    "solana-testnet": {
        "rpc_url": "https://api.testnet.solana.com",
        "commitment": "confirmed",
        "token_mint": "",
    },
    "solana-mainnet": {
        "rpc_url": "https://api.mainnet-beta.solana.com",
        "commitment": "finalized",
        "token_mint": "",
    },
}

KNOWN_CHAINS = frozenset(_DEFAULTS.keys())


def get_chain_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return active chain configuration dict.

    Reads [blockchain] section.  Merges built-in defaults with any
    operator-supplied overrides from [blockchain.networks.<chain>].

    Returns:
        {
            "chain_id":   str,   # e.g. "solana-devnet"
            "rpc_url":    str,
            "commitment": str,   # "confirmed" | "finalized" | "processed"
            "token_mint": str,   # base58 mint address or ""
        }

    Raises:
        ValueError: if the configured chain is not in KNOWN_CHAINS.
    """
    blockchain_cfg = config.get("blockchain", {})
    chain_id = str(blockchain_cfg.get("chain", "solana-devnet")).strip()

    if chain_id not in KNOWN_CHAINS:
        raise ValueError(
            f"Unknown blockchain chain: {chain_id!r}. "
            f"Known chains: {sorted(KNOWN_CHAINS)}"
        )

    # Start from built-in defaults
    result: Dict[str, Any] = dict(_DEFAULTS[chain_id])
    result["chain_id"] = chain_id

    # Layer operator overrides from [blockchain.networks.<chain_id>]
    networks = blockchain_cfg.get("networks", {})
    overrides = networks.get(chain_id, {})
    for key in ("rpc_url", "commitment", "token_mint"):
        if key in overrides:
            result[key] = str(overrides[key])

    logger.debug(
        f"CHAIN_CONFIG chain={chain_id} rpc={result['rpc_url']!r} "
        f"commitment={result['commitment']!r}"
    )
    return result
