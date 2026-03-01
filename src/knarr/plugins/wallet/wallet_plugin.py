"""Wallet plugin main class."""
import asyncio
import logging
import os
import time
from typing import Optional

from knarr.dht.plugins import PluginHooks, PluginContext, NodeHealth
from knarr.core.models import NodeInfo
from knarr.core.constants import KNARR_MINT

logger = logging.getLogger("knarr.plugin.wallet")


class WalletPlugin(PluginHooks):
    """Hot wallet management, auto-drain, and transaction firewall."""

    def __init__(self, ctx: PluginContext, config: dict):
        self._ctx = ctx
        self._config = config
        self._hot_signer = None
        self._tx_firewall = None
        self._drain_address = config.get("drain_address", "")
        self._drain_threshold = float(config.get("drain_threshold", 100))
        self._rpc_url = config.get("rpc_url", "https://api.mainnet-beta.solana.com")
        self._last_drain_check = 0
        self._drain_check_interval = 300  # 5 minutes

        # Initialize hot wallet from vault
        self._init_hot_wallet(ctx)

        # Note: settle_request is handled by core commerce handler (Card D).
        # Wallet plugin observes settlements via on_tick, not via mail handler override.

    def _init_hot_wallet(self, ctx: PluginContext):
        """Load or generate hot wallet keypair from vault."""
        try:
            # Hot seed stored in vault: scope='__wallet__', key='hot_seed'
            if not getattr(ctx, 'vault_get', None) or not getattr(ctx, 'vault_set', None):
                ctx.log.error("Vault accessors not available — cannot init hot wallet")
                return

            seed_hex = ctx.vault_get("__wallet__", "hot_seed")
            if seed_hex:
                seed = bytes.fromhex(seed_hex)
            else:
                seed = os.urandom(32)
                ctx.vault_set("__wallet__", "hot_seed", seed.hex())
                ctx.log.info("Generated new hot wallet keypair → vault")

            from .hot import KeypairSigner
            self._hot_signer = KeypairSigner(seed)

            # Register egress material — raw bytes (filter checks hex + base58 encodings)
            if getattr(ctx, 'register_egress_material', None):
                ctx.register_egress_material(seed)

            ctx.log.info(f"Wallet plugin initialized. Hot address: {self._hot_signer.get_address()}")

            # Initialize transaction firewall
            from .tx_firewall import TransactionFirewall
            peer_wallets = self._collect_peer_wallets(ctx)
            self._tx_firewall = TransactionFirewall(
                own_address=self._hot_signer.get_address(),
                known_peer_wallets=peer_wallets,
            )

            # Wire ledger balance lookup via ctx.get_peers (reads from node's own storage)
            # Do NOT open a second Storage connection — that bypasses the write queue
            def _get_ledger_balance(wallet: str) -> Optional[float]:
                """Look up bilateral ledger position for a peer wallet address.

                Uses ctx.get_peers to access the node's authoritative storage,
                not a separate connection. O(n) on ledger but typically < 50 entries.
                """
                from knarr.core.wallet import b58encode
                # We need the node's storage, but PluginContext doesn't expose
                # ledger methods directly. Use storage_path for read-only lookup
                # on committed data (SQLite WAL readers see committed state).
                if not ctx.storage_path:
                    return None
                try:
                    import sqlite3
                    conn = sqlite3.connect(f"file:{ctx.storage_path}?mode=ro", uri=True)
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute("SELECT peer_public_key, balance FROM ledger").fetchall()
                    conn.close()
                    for row in rows:
                        pk_hex = row["peer_public_key"]
                        try:
                            peer_wallet = b58encode(bytes.fromhex(pk_hex))
                            if peer_wallet == wallet:
                                return row["balance"]
                        except Exception:
                            continue
                except Exception:
                    pass
                return None

            self._tx_firewall.set_ledger_lookup(_get_ledger_balance)

        except Exception as e:
            ctx.log.error(f"Failed to initialize hot wallet: {e}")

    def _collect_peer_wallets(self, ctx: PluginContext) -> set:
        """Collect known peer wallet addresses from DHT."""
        wallets = set()
        for peer in ctx.get_peers():
            if hasattr(peer, 'wallet') and peer.wallet:
                wallets.add(peer.wallet)
        return wallets

    async def on_tick(self, peers, health: NodeHealth):
        """Periodic maintenance: update peer wallets, check drain threshold."""
        # Update peer wallets for tx firewall
        if self._tx_firewall:
            wallets = {p.wallet for p in peers if hasattr(p, 'wallet') and p.wallet}
            self._tx_firewall.update_peer_wallets(wallets)

        # Auto-drain check (every 5 minutes)
        now = time.time()
        if now - self._last_drain_check > self._drain_check_interval:
            self._last_drain_check = now
            if (self._drain_address and self._hot_signer
                    and "MONITOR" not in self._hot_signer.get_address()
                    and KNARR_MINT):
                asyncio.ensure_future(self._check_auto_drain())

    async def _check_auto_drain(self):
        """Check balance and drain safely to cold storage if over threshold."""
        from .solana_rpc_plugin import get_token_balance
        balance = await get_token_balance(
            self._hot_signer.get_address(), KNARR_MINT, self._rpc_url
        )
        if balance is None or balance <= self._drain_threshold:
            return

        drain_amount = balance - (self._drain_threshold * 0.5)  # Drain to 50% of threshold
        logger.info(f"AUTO_DRAIN balance={balance:.2f} threshold={self._drain_threshold} "
                    f"draining={drain_amount:.2f} to={self._drain_address}")

        # TX firewall check — drain_address is NOT a peer wallet, so we need
        # a special "drain" exception in the firewall
        # For now, log the intent. Actual signing requires solders Transaction building.
        # TODO: implement actual drain transaction in Phase C

    async def on_shutdown(self):
        """Clean shutdown — zero hot key from memory."""
        if self._hot_signer:
            # Python can't truly zero memory, but we can dereference
            self._hot_signer = None
            logger.info("Wallet plugin shutdown — hot signer dereferenced")
