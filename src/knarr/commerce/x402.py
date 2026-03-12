"""x402 storefront payment helpers."""

from __future__ import annotations

import base64
import hashlib
import time
from typing import Any

from ..core.wallet import b58encode
from ..plugins.wallet.solana_rpc_plugin import submit_transaction

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TRANSFER_CHECKED_OPCODE = 12
REPLAY_TTL_SECONDS = 120
SOLANA_MAX_TX_BYTES = 1232

_CAIP2_BY_CHAIN = {
    "solana-devnet": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
    "solana-testnet": "solana:4uhcVJyU9pJkvQyS88uRDiswHXSCkY3z",
    "solana-mainnet": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
}
_REPLAY_CACHE: dict[str, float] = {}

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def _cleanup_replay_cache(now: float | None = None) -> None:
    now = time.time() if now is None else now
    stale = [key for key, ts in _REPLAY_CACHE.items() if (now - ts) > REPLAY_TTL_SECONDS]
    for key in stale:
        _REPLAY_CACHE.pop(key, None)


def _b58decode(value: str) -> bytes:
    number = 0
    for char in value:
        if char not in _B58_INDEX:
            raise ValueError(f"invalid base58 character: {char!r}")
        number = (number * 58) + _B58_INDEX[char]
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    pad = 0
    for char in value:
        if char == "1":
            pad += 1
        else:
            break
    return (b"\x00" * pad) + raw


def _read_shortvec(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("shortvec truncated")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, offset
        shift += 7
        if shift > 28:
            raise ValueError("shortvec too large")


def _parse_legacy_transaction(tx_bytes: bytes) -> dict[str, Any]:
    sig_count, offset = _read_shortvec(tx_bytes, 0)
    offset += sig_count * 64
    if offset >= len(tx_bytes):
        raise ValueError("transaction missing message")

    header = tx_bytes[offset:offset + 3]
    if len(header) != 3:
        raise ValueError("transaction header truncated")
    offset += 3

    account_count, offset = _read_shortvec(tx_bytes, offset)
    account_keys = []
    for _ in range(account_count):
        key = tx_bytes[offset:offset + 32]
        if len(key) != 32:
            raise ValueError("account key truncated")
        account_keys.append(b58encode(key))
        offset += 32

    recent_blockhash = tx_bytes[offset:offset + 32]
    if len(recent_blockhash) != 32:
        raise ValueError("recent blockhash truncated")
    offset += 32

    instruction_count, offset = _read_shortvec(tx_bytes, offset)
    instructions = []
    for _ in range(instruction_count):
        if offset >= len(tx_bytes):
            raise ValueError("instruction truncated")
        program_index = tx_bytes[offset]
        offset += 1

        account_index_count, offset = _read_shortvec(tx_bytes, offset)
        account_indices = list(tx_bytes[offset:offset + account_index_count])
        if len(account_indices) != account_index_count:
            raise ValueError("instruction account list truncated")
        offset += account_index_count

        data_len, offset = _read_shortvec(tx_bytes, offset)
        data = tx_bytes[offset:offset + data_len]
        if len(data) != data_len:
            raise ValueError("instruction data truncated")
        offset += data_len

        instructions.append({
            "program_index": program_index,
            "account_indices": account_indices,
            "data": data,
        })

    if offset != len(tx_bytes):
        raise ValueError(f"trailing bytes: parsed {offset}, total {len(tx_bytes)}")

    return {
        "account_keys": account_keys,
        "fee_payer": account_keys[0] if account_keys else "",
        "recent_blockhash": b58encode(recent_blockhash),
        "instructions": instructions,
    }


def _normalize_atomic_amount(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("expected_amount must be numeric")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("expected_amount must be atomic units")
        return int(value)
    return int(str(value).strip())


def build_payment_required(chain_config: dict, exposure_config: dict, wallet_address: str) -> dict:
    """Build an x402 v2 PaymentRequired response body."""
    if not wallet_address:
        raise ValueError("wallet_address is required for x402")

    chain_id = str(chain_config.get("chain_id", "solana-devnet"))
    network = chain_config.get("caip2_network") or _CAIP2_BY_CHAIN.get(chain_id, _CAIP2_BY_CHAIN["solana-devnet"])
    amount = exposure_config.get("payment_amount")
    if amount in ("", None):
        raise ValueError("payment_amount is required for x402")

    assets = exposure_config.get("payment_assets") or []
    if not assets:
        asset = exposure_config.get("payment_asset") or chain_config.get("token_mint") or ""
        assets = [asset]

    pay_to = exposure_config.get("payment_address") or wallet_address
    timeout_seconds = int(exposure_config.get("payment_timeout_seconds", 60) or 60)

    accepts = []
    for asset_entry in assets:
        if isinstance(asset_entry, dict):
            asset = asset_entry.get("asset") or asset_entry.get("mint") or ""
            asset_amount = asset_entry.get("amount", amount)
            destination = asset_entry.get("payTo") or asset_entry.get("payment_address") or pay_to
        else:
            asset = str(asset_entry or "")
            asset_amount = amount
            destination = pay_to
        if not asset:
            raise ValueError("payment_asset is required for x402")
        accepts.append({
            "scheme": "exact",
            "network": network,
            "amount": str(_normalize_atomic_amount(asset_amount)),
            "asset": asset,
            "payTo": destination,
            "maxTimeoutSeconds": timeout_seconds,
            "extra": {"feePayer": wallet_address},
        })

    return {"x402Version": 2, "accepts": accepts}


def verify_x402_payload(
    payment_header: str,
    expected_amount: Any,
    expected_asset: str,
    expected_destination: str,
    node_address: str,
) -> dict[str, Any]:
    """Verify a raw x402 Solana transaction payload."""
    try:
        tx_bytes = base64.b64decode(payment_header, validate=True)
    except Exception:
        return {"verified": False, "error": "payment-signature must be base64 transaction bytes"}

    if len(tx_bytes) > SOLANA_MAX_TX_BYTES:
        return {"verified": False, "error": f"transaction exceeds {SOLANA_MAX_TX_BYTES} byte limit"}

    digest = hashlib.sha256(tx_bytes).hexdigest()
    _cleanup_replay_cache()
    if digest in _REPLAY_CACHE:
        return {"verified": False, "error": "replay detected"}

    try:
        parsed = _parse_legacy_transaction(tx_bytes)
    except Exception as exc:
        return {"verified": False, "error": f"invalid transaction: {exc}"}

    # Reject multi-instruction transactions — only a single TransferChecked is permitted.
    # Extra instructions could drain the node's SOL or tokens via smuggled transfers.
    if len(parsed["instructions"]) != 1:
        return {"verified": False, "error": f"expected exactly 1 instruction, got {len(parsed['instructions'])}"}

    # Fee payer safety: node must never be the fee payer for client-submitted transactions
    if node_address and parsed["fee_payer"] == node_address:
        return {"verified": False, "error": "fee payer safety violation: node is fee payer"}

    instruction = parsed["instructions"][0]
    try:
        program_id = parsed["account_keys"][instruction["program_index"]]
    except (IndexError, KeyError):
        return {"verified": False, "error": "invalid program index"}

    if program_id not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
        return {"verified": False, "error": "instruction is not SPL Token TransferChecked"}

    data = instruction["data"]
    if not data or data[0] != TRANSFER_CHECKED_OPCODE:
        return {"verified": False, "error": "missing TransferChecked instruction"}

    token_program = program_id
    account_keys = parsed["account_keys"]
    instruction_accounts = [account_keys[idx] for idx in instruction["account_indices"] if idx < len(account_keys)]

    # Check node address is not in the instruction's account list (source, mint, dest, authority)
    if node_address and node_address in instruction_accounts:
        return {"verified": False, "error": "fee payer safety violation"}
    if len(instruction_accounts) < 3:
        return {"verified": False, "error": "TransferChecked accounts incomplete"}

    mint = instruction_accounts[1]
    destination = instruction_accounts[2]
    if not expected_asset:
        return {"verified": False, "error": "expected_asset is required"}
    if mint != expected_asset:
        return {"verified": False, "error": "asset mismatch"}
    if not expected_destination:
        return {"verified": False, "error": "expected_destination is required"}
    if destination != expected_destination:
        return {"verified": False, "error": "destination mismatch"}

    if len(data) < 10:
        return {"verified": False, "error": "TransferChecked payload truncated"}
    try:
        amount = int.from_bytes(data[1:9], "little")
        expected_atomic = _normalize_atomic_amount(expected_amount)
    except Exception as exc:
        return {"verified": False, "error": str(exc)}
    if amount != expected_atomic:
        return {"verified": False, "error": "amount mismatch"}

    _REPLAY_CACHE[digest] = time.time()
    return {
        "verified": True,
        "tx_bytes": tx_bytes,
        "digest": digest,
        "amount": amount,
        "asset": mint,
        "destination": destination,
        "program_id": token_program,
        "fee_payer": parsed["fee_payer"],
    }


async def settle_x402(tx_bytes: bytes, signing_key, rpc_url: str) -> dict[str, Any]:
    """Submit a verified x402 transaction via Solana RPC."""
    if not tx_bytes:
        return {"success": False, "error": "empty transaction bytes"}
    try:
        signature = await submit_transaction(tx_bytes, rpc_url=rpc_url)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if not signature:
        return {"success": False, "error": "sendTransaction returned no signature"}
    return {"success": True, "signature": signature}
