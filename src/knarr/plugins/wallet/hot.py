"""Hot wallet signer interface.

WalletSigner is the abstract base; KeypairSigner is the shipping implementation
using PyNaCl (already a Knarr dependency). SquadsMultisigSigner is a future
extension point for multisig governance wallets.
"""
from abc import ABC, abstractmethod

from nacl.signing import SigningKey

from knarr.core.wallet import derive_solana_address


class WalletSigner(ABC):
    """Abstract signer interface for Knarr wallet operations."""

    @abstractmethod
    def sign_message(self, message: bytes) -> bytes:
        """Sign arbitrary bytes. Returns 64-byte Ed25519 signature."""
        ...

    @abstractmethod
    def get_address(self) -> str:
        """Return the base58 Solana address for this signer."""
        ...


class KeypairSigner(WalletSigner):
    """Ed25519 keypair signer using PyNaCl (zero external deps beyond Knarr core)."""

    def __init__(self, seed: bytes):
        if len(seed) != 32:
            raise ValueError("Seed must be exactly 32 bytes")
        self._signing_key = SigningKey(seed)
        self._address = derive_solana_address(self._signing_key)

    def sign_message(self, message: bytes) -> bytes:
        """Sign message bytes. Returns raw 64-byte Ed25519 signature."""
        signed = self._signing_key.sign(message)
        return signed.signature

    def get_address(self) -> str:
        """Return base58-encoded Solana address (Ed25519 public key)."""
        return self._address
