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

from .constants import KNARR_DECIMALS, KNARR_MINT, KNARR_SYMBOL

logger = logging.getLogger(__name__)

# ── Built-in network defaults ──────────────────────────────────────────
_DEFAULTS: Dict[str, Dict[str, str]] = {
    "solana-devnet": {
        "rpc_url": "https://api.devnet.solana.com",
        "commitment": "confirmed",
        "token_mint": "",
        "caip2_network": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
    },
    "solana-testnet": {
        "rpc_url": "https://api.testnet.solana.com",
        "commitment": "confirmed",
        "token_mint": "",
        "caip2_network": "solana:4uhcVJyU9pJkvQyS88uRDiswHXSCkY3z",
    },
    "solana-mainnet": {
        "rpc_url": "https://api.mainnet-beta.solana.com",
        "commitment": "finalized",
        "token_mint": "",
        "caip2_network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    },
}

KNOWN_CHAINS = frozenset(_DEFAULTS.keys())
_DEFAULT_TOKEN_CONFIGS: Dict[str, Dict[str, Any]] = {
    KNARR_SYMBOL: {
        "mint": KNARR_MINT,
        "decimals": KNARR_DECIMALS,
        "symbol": KNARR_SYMBOL,
    }
}


def _parse_token_decimals(raw: Any, symbol: str) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"Token decimals for {symbol} must be an integer")
    value = int(raw)
    if value < 0:
        raise ValueError(f"Token decimals for {symbol} must be >= 0")
    return value


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
    for key in ("rpc_url", "commitment", "token_mint", "caip2_network"):
        if key in overrides:
            result[key] = str(overrides[key])

    token_symbol = str(
        blockchain_cfg.get("token", blockchain_cfg.get("token_symbol", KNARR_SYMBOL))
    ).strip().upper() or KNARR_SYMBOL
    token_registry = {}
    for configured_symbol in set(list(_DEFAULT_TOKEN_CONFIGS.keys()) + list((blockchain_cfg.get("tokens", {}) or {}).keys()) + [token_symbol]):
        token_registry[configured_symbol] = get_token_config(config, configured_symbol)
    token_cfg = get_token_config(config, token_symbol)
    if not result.get("token_mint"):
        result["token_mint"] = token_cfg["mint"]
    result["token_symbol"] = token_cfg["symbol"]
    result["token_decimals"] = token_cfg["decimals"]
    result["tokens"] = token_registry

    logger.debug(
        f"CHAIN_CONFIG chain={chain_id} rpc={result['rpc_url']!r} "
        f"commitment={result['commitment']!r} token={result['token_symbol']!r}"
    )
    return result


def get_token_config(config: dict, symbol: str = KNARR_SYMBOL) -> dict:
    """Return {mint, decimals, symbol} for a configured token.
    Reads [blockchain.tokens.<symbol>]. Falls back to constants."""
    lookup_symbol = str(symbol or KNARR_SYMBOL).strip().upper() or KNARR_SYMBOL
    blockchain_cfg = config.get("blockchain", {})
    tokens_cfg = blockchain_cfg.get("tokens", {}) or {}
    token_cfg = tokens_cfg.get(lookup_symbol, {}) or {}
    defaults = _DEFAULT_TOKEN_CONFIGS.get(lookup_symbol, {})

    if not token_cfg and not defaults:
        raise ValueError(f"Unknown blockchain token: {lookup_symbol!r}")

    result = {
        "mint": str(token_cfg.get("mint", defaults.get("mint", ""))).strip(),
        "decimals": _parse_token_decimals(
            token_cfg.get("decimals", defaults.get("decimals", 0)),
            lookup_symbol,
        ),
        "symbol": str(token_cfg.get("symbol", defaults.get("symbol", lookup_symbol))).strip() or lookup_symbol,
    }

    logger.debug(
        f"TOKEN_CONFIG symbol={lookup_symbol} mint={result['mint']!r} "
        f"decimals={result['decimals']}"
    )
    return result
