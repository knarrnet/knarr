import base64

import pytest

from knarr.commerce import x402
from knarr.commerce.x402 import build_payment_required, settle_x402, verify_x402_payload
from knarr.core.wallet import b58encode


def _shortvec(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _build_transfer_checked_tx(amount: int, *, destination: bytes, mint: bytes, source: bytes | None = None) -> bytes:
    payer = bytes([1]) * 32
    source = source or (bytes([2]) * 32)
    authority = bytes([4]) * 32
    program = x402._b58decode(x402.TOKEN_PROGRAM_ID)
    accounts = [payer, source, mint, destination, authority, program]

    message = bytearray()
    message.extend(b"\x01\x00\x01")
    message.extend(_shortvec(len(accounts)))
    for account in accounts:
        message.extend(account)
    message.extend(bytes([9]) * 32)
    message.extend(_shortvec(1))
    message.append(5)
    message.extend(_shortvec(4))
    message.extend(bytes([1, 2, 3, 4]))
    data = bytes([12]) + amount.to_bytes(8, "little") + bytes([9])
    message.extend(_shortvec(len(data)))
    message.extend(data)
    return _shortvec(1) + (bytes([7]) * 64) + bytes(message)


def test_build_payment_required_uses_caip2_and_exact_scheme():
    result = build_payment_required(
        {"chain_id": "solana-mainnet", "token_mint": "Mint111", "caip2_network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"},
        {"payment": "x402", "payment_amount": "42", "payment_asset": "Mint111"},
        "Wallet111",
    )
    assert result["x402Version"] == 2
    assert result["accepts"][0]["scheme"] == "exact"
    assert result["accepts"][0]["network"].startswith("solana:")
    assert result["accepts"][0]["asset"] == "Mint111"


def test_verify_x402_payload_accepts_valid_transfer_checked():
    x402._REPLAY_CACHE.clear()
    destination = bytes([3]) * 32
    mint = bytes([8]) * 32
    tx = _build_transfer_checked_tx(42, destination=destination, mint=mint)
    result = verify_x402_payload(
        base64.b64encode(tx).decode("ascii"),
        42,
        b58encode(mint),
        b58encode(destination),
        b58encode(bytes([11]) * 32),
    )
    assert result["verified"] is True
    assert result["amount"] == 42


def test_verify_x402_payload_rejects_wrong_amount():
    x402._REPLAY_CACHE.clear()
    destination = bytes([3]) * 32
    mint = bytes([8]) * 32
    tx = _build_transfer_checked_tx(41, destination=destination, mint=mint)
    result = verify_x402_payload(
        base64.b64encode(tx).decode("ascii"),
        42,
        b58encode(mint),
        b58encode(destination),
        b58encode(bytes([11]) * 32),
    )
    assert result["verified"] is False
    assert result["error"] == "amount mismatch"


def test_verify_x402_payload_rejects_replay():
    x402._REPLAY_CACHE.clear()
    destination = bytes([3]) * 32
    mint = bytes([8]) * 32
    header = base64.b64encode(_build_transfer_checked_tx(42, destination=destination, mint=mint)).decode("ascii")
    first = verify_x402_payload(header, 42, b58encode(mint), b58encode(destination), b58encode(bytes([11]) * 32))
    second = verify_x402_payload(header, 42, b58encode(mint), b58encode(destination), b58encode(bytes([11]) * 32))
    assert first["verified"] is True
    assert second["verified"] is False
    assert second["error"] == "replay detected"


def test_verify_x402_payload_rejects_fee_payer_safety_violation():
    x402._REPLAY_CACHE.clear()
    destination = bytes([11]) * 32
    mint = bytes([8]) * 32
    tx = _build_transfer_checked_tx(42, destination=destination, mint=mint)
    node_address = b58encode(bytes([11]) * 32)
    result = verify_x402_payload(
        base64.b64encode(tx).decode("ascii"),
        42,
        b58encode(mint),
        b58encode(destination),
        node_address,
    )
    assert result["verified"] is False
    assert result["error"] == "fee payer safety violation"


@pytest.mark.asyncio
async def test_settle_x402_calls_submit_transaction(monkeypatch):
    async def fake_submit(tx_bytes, rpc_url=""):
        assert tx_bytes == b"tx"
        assert rpc_url == "https://rpc.example"
        return "sig123"

    monkeypatch.setattr("knarr.commerce.x402.submit_transaction", fake_submit)
    result = await settle_x402(b"tx", object(), "https://rpc.example")
    assert result == {"success": True, "signature": "sig123"}
