"""Ed25519 → Solana wallet address derivation.

Solana uses Ed25519 natively. The node's identity keypair IS a valid
Solana keypair. This module encodes the public key as base58 — the
standard Solana address format.
"""

# Base58 alphabet (Bitcoin/Solana standard)
_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_DECODE = {chr(c): i for i, c in enumerate(_B58_ALPHABET)}


def b58encode(data: bytes) -> str:
    """Base58 encode raw bytes (no checksum). Standard Bitcoin/Solana alphabet."""
    n = int.from_bytes(data, "big")
    result = []
    while n > 0:
        n, r = divmod(n, 58)
        result.append(_B58_ALPHABET[r:r + 1])
    # Preserve leading zero bytes
    for b in data:
        if b == 0:
            result.append(b"1")
        else:
            break
    return b"".join(reversed(result)).decode("ascii")


def b58decode(data: str) -> bytes:
    """Base58 decode string (no checksum). Standard Bitcoin/Solana alphabet."""
    n = 0
    for c in data:
        n = n * 58 + _B58_DECODE[c]
    result = n.to_bytes((n.bit_length() + 7) // 8, "big") if n > 0 else b""
    leading_zeros = len(data) - len(data.lstrip("1"))
    return b"\x00" * leading_zeros + result


def derive_solana_address(signing_key) -> str:
    """Derive Solana address from a PyNaCl SigningKey.

    Args:
        signing_key: nacl.signing.SigningKey instance
    Returns:
        Base58-encoded Solana address (32-44 chars)
    """
    pubkey_bytes = signing_key.verify_key.encode()  # 32 bytes
    return b58encode(pubkey_bytes)
