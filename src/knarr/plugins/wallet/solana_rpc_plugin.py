"""Read-only Solana RPC client for balance queries.

Uses stdlib urllib — no async HTTP library. All calls run in executor
to avoid blocking the event loop.
"""
import asyncio
import json
import logging
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
_RPC_TIMEOUT = 10  # seconds
_ALLOWED_SCHEMES = {"https", "http"}


def _validate_rpc_url(url: str) -> str:
    """Validate RPC URL scheme. Returns url or raises ValueError."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"RPC URL scheme must be https or http, got: {parsed.scheme!r}")
    return url


def _rpc_call(rpc_url: str, method: str, params: list) -> dict:
    """Synchronous JSON-RPC call. Runs in executor."""
    _validate_rpc_url(rpc_url)
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": method, "params": params
    }).encode()
    req = urllib.request.Request(
        rpc_url, data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_RPC_TIMEOUT) as resp:
        return json.loads(resp.read())


def _parse_token_amount(account: dict) -> float:
    """Parse token amount from a single account, with fallback chain.

    Priority: uiAmount (float) > uiAmountString > amount/decimals.
    """
    try:
        info = account["account"]["data"]["parsed"]["info"]["tokenAmount"]
        # 1. uiAmount when numeric
        ui = info.get("uiAmount")
        if ui is not None and isinstance(ui, (int, float)):
            return float(ui)
        # 2. uiAmountString
        ui_str = info.get("uiAmountString")
        if ui_str:
            return float(ui_str)
        # 3. amount / 10^decimals
        raw = int(info.get("amount", 0))
        decimals = int(info.get("decimals", 0))
        return raw / (10 ** decimals) if decimals else float(raw)
    except (KeyError, ValueError, TypeError):
        return 0.0


async def get_token_balance(
    wallet: str, mint: str, rpc_url: str = DEFAULT_RPC_URL
) -> Optional[float]:
    """Get SPL token balance (sum of all token accounts). Returns None on failure."""
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _rpc_call, rpc_url,
            "getTokenAccountsByOwner",
            [wallet, {"mint": mint}, {"encoding": "jsonParsed"}])
        if "error" in data:
            logger.debug(f"Token balance RPC error: {data['error']}")
            return None
        accounts = data.get("result", {}).get("value", [])
        if not accounts:
            return 0.0
        return sum(_parse_token_amount(a) for a in accounts)
    except Exception as e:
        logger.debug(f"Token balance query failed: {e}")
        return None


async def get_sol_balance(
    wallet: str, rpc_url: str = DEFAULT_RPC_URL
) -> Optional[float]:
    """Get SOL balance (for tx fee gauge). Returns None on failure."""
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _rpc_call, rpc_url,
            "getBalance", [wallet])
        if "error" in data:
            logger.debug(f"SOL balance RPC error: {data['error']}")
            return None
        lamports = data.get("result", {}).get("value", 0)
        return lamports / 1e9
    except Exception as e:
        logger.debug(f"SOL balance query failed: {e}")
        return None


async def submit_transaction(tx_bytes: bytes, rpc_url: str = DEFAULT_RPC_URL) -> Optional[str]:
    """Submit signed transaction to network (Phase C stub)."""
    # For now, just log and return None
    logger.info(f"submit_transaction stub called, bytes_len={len(tx_bytes)}")
    return None
