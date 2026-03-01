"""Transaction Firewall."""
import logging
from typing import Set

logger = logging.getLogger("knarr.plugin.wallet.firewall")

# Solana program IDs (base58)
ALLOWED_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated Token Account
    "11111111111111111111111111111111",              # System Program
}

class TransactionFirewall:
    """Validates transactions before signing. Rejects anything outside the whitelist."""

    def __init__(self, own_address: str, known_peer_wallets: Set[str]):
        self._own_address = own_address
        self._known_peer_wallets = known_peer_wallets
        self._get_ledger_balance = None  # Set by plugin after init

    def set_ledger_lookup(self, fn):
        """Set the function to look up bilateral ledger balance."""
        self._get_ledger_balance = fn

    def update_peer_wallets(self, wallets: Set[str]):
        """Update known peer wallet set (called on heartbeat/announce)."""
        self._known_peer_wallets = wallets

    def check(self, program_id: str, source: str, destination: str, amount: float) -> tuple[bool, str]:
        """
        Four-check whitelist. Returns (allowed, reason).
        ALL checks must pass. Failure = CRITICAL log + refuse to sign.
        """
        # Check 1: Program whitelist
        if program_id not in ALLOWED_PROGRAMS:
            return False, f"program not whitelisted: {program_id}"

        # Check 2: Source = own hot wallet
        if source != self._own_address:
            return False, f"source is not own wallet: {source}"

        # Check 3: Destination = known peer wallet from DHT
        # Drain address exception for Phase C
        if destination not in self._known_peer_wallets:
            return False, f"destination not a known peer: {destination}"

        # Check 4: Amount ≤ bilateral ledger position (FAIL-CLOSED if lookup missing)
        if not self._get_ledger_balance:
            return False, "ledger lookup not wired — refusing to sign"
        balance = self._get_ledger_balance(destination)
        if balance is None:
            return False, f"no ledger entry for destination {destination}"
        if amount > abs(balance):
            return False, f"amount {amount} exceeds ledger position {balance}"

        return True, "ok"
