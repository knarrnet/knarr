import asyncio
import hashlib
import importlib
import json
import logging
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from knarr.core.crypto import SigningKey

from knarr.commerce.transfer_event import ConfirmationStatus, TransferEvent
from knarr.core.wallet import b58decode, b58encode, derive_solana_address
from knarr.dht.plugins import NodeHealth, PluginContext, PluginHooks

try:
    from .solana import PollResult, SolanaWatcher, SolanaSubscriptionManager
except ImportError:  # PluginLoader injects sibling modules as top-level names
    from solana import PollResult, SolanaWatcher, SolanaSubscriptionManager

log = logging.getLogger("knarr.plugin.bcw")

_PREFIX_BY_TYPE = {
    "payment_received": "prx",
    "payment_finalized": "pfin",
    "payment_executed": "pexe",
    "wallet_transfer": "wtfr",
    "wallet_withdrawal": "wwdr",
}

_SUPPORTED_SOLANA_CHAIN_IDS = {
    "solana-devnet",
    "solana-testnet",
    "solana-mainnet",
}
_DEFAULT_RPC_BY_CHAIN = {
    "solana-devnet": "https://api.devnet.solana.com",
    "solana-testnet": "https://api.testnet.solana.com",
}
_LAMPORTS_PER_SOL = 1_000_000_000

# BCW-01: Token-2022 ATA derivation constants
_TOKEN_2022_PROGRAM_ID_B58 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
_ATA_PROGRAM_ID_B58 = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe8bv"


def derive_counterparty_address(master_seed: bytes, node_id: str, chain_id: str) -> str:
    if len(node_id) != 64:
        raise ValueError(f"node_id must be 64 hex chars, got {len(node_id)}")
    try:
        bytes.fromhex(node_id)
    except ValueError:
        raise ValueError(f"node_id must be valid hex, got {node_id[:16]}...")
    seed = hashlib.sha256(master_seed + node_id.encode("utf-8")).digest()
    if not isinstance(chain_id, str):
        raise ValueError(f"Unsupported chain: {chain_id}")
    if chain_id in _SUPPORTED_SOLANA_CHAIN_IDS:
        return derive_solana_address(SigningKey(seed))
    raise ValueError(f"Unsupported chain: {chain_id}")


def _derive_master_address(master_seed: bytes, chain_id: str) -> str:
    if not isinstance(chain_id, str):
        raise ValueError(f"Unsupported chain: {chain_id}")
    if chain_id in _SUPPORTED_SOLANA_CHAIN_IDS:
        return derive_solana_address(SigningKey(master_seed))
    raise ValueError(f"Unsupported chain: {chain_id}")


def _find_program_address(seeds: list[bytes], program_id_bytes: bytes) -> bytes:
    """Derive a Solana Program Derived Address (PDA).

    Iterates nonce 255→0; returns the first SHA-256 hash that is NOT a valid
    Ed25519 curve point, per the Solana runtime spec.
    """
    from knarr.core.crypto import crypto_core_ed25519_is_valid_point
    for nonce in range(255, -1, -1):
        h = hashlib.sha256()
        for s in seeds:
            h.update(s)
        h.update(bytes([nonce]))
        h.update(program_id_bytes)
        h.update(b"ProgramDerivedAddress")
        candidate = h.digest()
        if not crypto_core_ed25519_is_valid_point(candidate):
            return candidate
    raise ValueError("Could not find program-derived address after 256 attempts")


def derive_ata(owner_b58: str, mint_b58: str) -> str:
    """Derive the Associated Token Account (ATA) address for a Token-2022 mint.

    SPL Token-2022 uses a different program ID than legacy SPL Token.
    Seeds: [owner_pubkey, token_2022_program_id, mint_pubkey]
    Program: ATA program ID.
    """
    owner_bytes = b58decode(owner_b58)
    mint_bytes = b58decode(mint_b58)
    token_prog_bytes = b58decode(_TOKEN_2022_PROGRAM_ID_B58)
    ata_prog_bytes = b58decode(_ATA_PROGRAM_ID_B58)
    addr_bytes = _find_program_address(
        [owner_bytes, token_prog_bytes, mint_bytes],
        ata_prog_bytes,
    )
    return b58encode(addr_bytes)


def _ws_url_from_rpc(rpc_url: str) -> str:
    if rpc_url.startswith("https://"):
        return "wss://" + rpc_url[len("https://"):]
    if rpc_url.startswith("http://"):
        return "ws://" + rpc_url[len("http://"):]
    return rpc_url


def _serialize_watch_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _parse_watch_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


class WatchStore:
    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bcw_watchlist (
                    node_id TEXT,
                    chain_id TEXT,
                    address TEXT,
                    last_signature TEXT,
                    PRIMARY KEY(node_id, chain_id)
                )
                """
            )
            for column_sql in (
                "ALTER TABLE bcw_watchlist ADD COLUMN token_filter TEXT",
                "ALTER TABLE bcw_watchlist ADD COLUMN requested_by TEXT",
                "ALTER TABLE bcw_watchlist ADD COLUMN correlation_id TEXT",
                "ALTER TABLE bcw_watchlist ADD COLUMN created_at REAL",
                "ALTER TABLE bcw_watchlist ADD COLUMN expires_at REAL",
                "ALTER TABLE bcw_watchlist ADD COLUMN last_seen REAL",
                "ALTER TABLE bcw_watchlist ADD COLUMN status TEXT NOT NULL DEFAULT 'watching'",
            ):
                try:
                    conn.execute(column_sql)
                except sqlite3.OperationalError:
                    pass
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bcw_seen (
                    chain_id TEXT,
                    tx_hash TEXT,
                    tx_index INTEGER,
                    PRIMARY KEY(chain_id, tx_hash, tx_index)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bcw_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    document_type TEXT NOT NULL,
                    dedup_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(dedup_key, document_type)
                )
                """
            )

    def upsert_watch(
        self,
        node_id: str,
        chain_id: str,
        address: str,
        *,
        token_filter: Optional[str] = None,
        requested_by: Optional[str] = None,
        correlation_id: Optional[str] = None,
        created_at: Optional[float] = None,
        expires_at: Optional[float] = None,
        last_seen: Optional[float] = None,
        status: str = "watching",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bcw_watchlist (
                    node_id, chain_id, address, last_signature, token_filter,
                    requested_by, correlation_id, created_at, expires_at, last_seen, status
                )
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id, chain_id)
                DO UPDATE SET
                    address=excluded.address,
                    token_filter=excluded.token_filter,
                    requested_by=excluded.requested_by,
                    correlation_id=excluded.correlation_id,
                    created_at=COALESCE(excluded.created_at, bcw_watchlist.created_at),
                    expires_at=excluded.expires_at,
                    last_seen=COALESCE(excluded.last_seen, bcw_watchlist.last_seen),
                    status=excluded.status
                """,
                (
                    node_id,
                    chain_id,
                    address,
                    token_filter,
                    requested_by,
                    correlation_id,
                    created_at,
                    expires_at,
                    last_seen,
                    status,
                ),
            )

    def remove_watch(self, node_id: str, chain_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM bcw_watchlist WHERE node_id=? AND chain_id=?",
                (node_id, chain_id),
            )

    def update_cursor(self, node_id: str, chain_id: str, last_signature: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE bcw_watchlist
                SET last_signature=?
                WHERE node_id=? AND chain_id=?
                """,
                (last_signature, node_id, chain_id),
            )

    def list_watches(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    node_id,
                    chain_id,
                    address,
                    last_signature,
                    token_filter,
                    requested_by,
                    correlation_id,
                    created_at,
                    expires_at,
                    last_seen,
                    status
                FROM bcw_watchlist
                ORDER BY chain_id, node_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_all_watches(self) -> list[dict]:
        return self.list_watches()

    def all_addresses(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT address FROM bcw_watchlist WHERE status = 'watching'"
            ).fetchall()
        return {row["address"] for row in rows if row["address"]}

    def get_node_id_for_address(self, address: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT node_id
                FROM bcw_watchlist
                WHERE address = ? AND status = 'watching'
                LIMIT 1
                """,
                (address,),
            ).fetchone()
        return row["node_id"] if row else None

    def get_address(self, node_id: str, chain_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT address FROM bcw_watchlist
                WHERE node_id = ? AND chain_id = ? AND status = 'watching'
                LIMIT 1
                """,
                (node_id, chain_id),
            ).fetchone()
        return row["address"] if row else None

    def update_status(self, node_id: str, chain_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE bcw_watchlist
                SET status=?
                WHERE node_id=? AND chain_id=?
                """,
                (status, node_id, chain_id),
            )

    def get_expired_watches(self, now: float) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    node_id,
                    chain_id,
                    address,
                    last_signature,
                    token_filter,
                    requested_by,
                    correlation_id,
                    created_at,
                    expires_at,
                    last_seen,
                    status
                FROM bcw_watchlist
                WHERE status = 'watching'
                  AND expires_at IS NOT NULL
                  AND expires_at < ?
                ORDER BY expires_at ASC
                """,
                (now,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_last_seen(self, node_id: str, chain_id: str, ts: float) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE bcw_watchlist
                SET last_seen=?
                WHERE node_id=? AND chain_id=?
                """,
                (ts, node_id, chain_id),
            )

    def activity_seen_since_watch(self, node_id: str, chain_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT created_at, last_seen
                FROM bcw_watchlist
                WHERE node_id=? AND chain_id=?
                LIMIT 1
                """,
                (node_id, chain_id),
            ).fetchone()
        if not row:
            return False
        created_at = row["created_at"]
        last_seen = row["last_seen"]
        return bool(
            created_at is not None
            and last_seen is not None
            and float(last_seen) >= float(created_at)
        )

    def mark_seen(self, chain_id: str, tx_hash: str, tx_index: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO bcw_seen (chain_id, tx_hash, tx_index)
                VALUES (?, ?, ?)
                """,
                (chain_id, tx_hash, tx_index),
            )
        return cur.rowcount > 0

    def write_receipt(
        self,
        receipt_id: str,
        document_type: str,
        dedup_key: str,
        payload_json: str,
    ) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO bcw_receipts
                (receipt_id, document_type, dedup_key, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (receipt_id, document_type, dedup_key, payload_json, _iso_now()),
            )
        return cur.rowcount > 0

    def latest_receipt_for(self, dedup_key: str, document_type: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT receipt_id
                FROM bcw_receipts
                WHERE dedup_key=? AND document_type=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (dedup_key, document_type),
            ).fetchone()
        return row["receipt_id"] if row else None


class BCWPlugin(PluginHooks):
    def __init__(self, ctx: PluginContext, config: dict):
        self._ctx = ctx
        self._config = dict(config or {})
        self._enabled = bool(self._config.get("enabled", True))
        self._poll_interval = int(self._config.get("poll_interval_seconds", 10))
        self._meta_published = False
        self._self_owned_addresses: set[str] = set()
        self._sub = None
        self._last_balance_check = 0.0
        self._balance_check_interval = 300.0
        self._subscription_managers: dict[str, SolanaSubscriptionManager] = {}
        self._ws_tasks: list[asyncio.Task] = []
        self._last_gap_recovery: dict[str, float] = {}
        self._ws_started = False
        self._ws_available = False
        self._signature_watch_requests: dict[tuple[str, str], dict[str, Optional[str]]] = {}

        self._store = WatchStore((ctx.state_dir or ctx.plugin_dir) / "bcw.sqlite3")
        self._solana_modules: dict[str, SolanaWatcher] = {}
        for chain_cfg in self._config.get("chains", []):
            normalized_cfg = dict(chain_cfg or {})
            chain_id = str(normalized_cfg.get("chain_id", ""))
            if chain_id not in _SUPPORTED_SOLANA_CHAIN_IDS:
                continue
            if chain_id == "solana-mainnet" and not str(normalized_cfg.get("rpc_url", "")).strip():
                self._log_warning("BCW mainnet chain requires explicit rpc_url: %s", chain_id)
                continue
            normalized_cfg.setdefault("rpc_url", _DEFAULT_RPC_BY_CHAIN.get(chain_id, ""))
            self._solana_modules[chain_id] = SolanaWatcher(chain_id, normalized_cfg)

        if self._enabled and not self._solana_modules:
            configured_chain_ids = ",".join(
                str((chain_cfg or {}).get("chain_id", "")).strip()
                for chain_cfg in self._config.get("chains", [])
            ).strip(",") or "<none>"
            self._enabled = False
            self._log_warning(
                "BCW disabled: no valid chains configured chain_id=%s",
                configured_chain_ids,
            )

        self._master_seed = self._load_master_seed()
        if self._enabled and not self._master_seed:
            self._enabled = False
            self._log_warning("BCW disabled: missing/invalid bcw_master_seed in vault")

        if self._enabled:
            try:
                importlib.import_module("websockets")
                self._ws_available = True
            except ImportError:
                self._log_warning("BCW websockets unavailable; HTTP gap recovery only")
                self._ws_available = False
            if self._ws_available:
                for chain_id, watcher in self._solana_modules.items():
                    manager = SolanaSubscriptionManager(chain_id, _ws_url_from_rpc(watcher.rpc_url))
                    manager.on_notification = self._on_ws_notification
                    manager._on_reconnect_callback = self._on_ws_reconnect
                    self._subscription_managers[chain_id] = manager

        subscribe = getattr(ctx, "subscribe_events", None)
        if callable(subscribe):
            try:
                self._sub = subscribe("bcw.watch_request", "bcw.unwatch", "bcw.poll", "peer.removed")
            except Exception as exc:
                self._log_warning("BCW event subscription failed: %s", exc)

        economy_cfg = self._economy_config()
        if "conversion_rate" not in economy_cfg:
            self._log_warning("conversion_rate is 1.0 (default) -- verify this is intentional")

    def _load_master_seed(self) -> Optional[bytes]:
        getter = getattr(self._ctx, "vault_get", None)
        if not callable(getter):
            return None

        value: Any = None
        for args in (("bcw_master_seed",), ("bcw", "bcw_master_seed")):
            try:
                value = getter(*args)
            except TypeError:
                continue
            except Exception as exc:
                self._log_warning("Vault read failed for %s: %s", args, exc)
                continue
            if value:
                break

        if value is None:
            return None
        if isinstance(value, bytes):
            seed = value
        elif isinstance(value, str):
            try:
                seed = bytes.fromhex(value)
            except ValueError:
                return None
        else:
            return None

        if len(seed) != 32:
            return None
        return seed

    def _queue_ws_task(self, coro) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        self._ws_tasks = [task for task in self._ws_tasks if not task.done()]
        self._ws_tasks.append(loop.create_task(coro))

    def _start_ws_managers(self) -> None:
        if self._ws_started or not self._ws_available:
            return
        self._ws_started = True
        for manager in self._subscription_managers.values():
            self._queue_ws_task(manager._reconnect_loop())

    def _selected_token_mints(self, watcher: SolanaWatcher, token_filter: Any) -> dict[str, str]:
        parsed_filter = _parse_watch_value(token_filter)
        if parsed_filter in (None, "", [], ()):
            return dict(watcher.token_mints)

        if isinstance(parsed_filter, str):
            requested = [parsed_filter]
        elif isinstance(parsed_filter, list):
            requested = [str(item) for item in parsed_filter]
        else:
            requested = [str(parsed_filter)]

        selected: dict[str, str] = {}
        for item in requested:
            token_name = item.strip()
            if not token_name:
                continue
            if token_name in watcher.token_mints:
                selected[token_name] = watcher.token_mints[token_name]
                continue
            for symbol, mint in watcher.token_mints.items():
                if mint == token_name:
                    selected[symbol] = mint
        return selected or dict(watcher.token_mints)

    def _register_ata_watches(
        self,
        node_id: str,
        chain_id: str,
        owner_address: str,
        watcher,
        token_filter: Any = None,
    ) -> list[str]:
        ata_addresses: list[str] = []
        for symbol, mint_address in self._selected_token_mints(watcher, token_filter).items():
            try:
                ata_addresses.append(derive_ata(owner_address, mint_address))
            except Exception as exc:
                self._log_warning(
                    "BCW: ATA derivation failed node=%s mint=%s: %s",
                    node_id[:8], symbol, exc,
                )
        return ata_addresses

    def _handle_bus_event(self, event: dict) -> None:
        etype = event.get("event")
        if etype == "bcw.watch_request":
            self._handle_watch_request(event)
            return
        if etype == "bcw.unwatch":
            node_id = str(event.get("node_id", "") or "")
            chain_id = str(event.get("chain_id", "solana-mainnet") or "solana-mainnet")
            if node_id:
                self._signature_watch_requests.pop((node_id, chain_id), None)
                manager = self._subscription_managers.get(chain_id)
                if manager is not None:
                    self._queue_ws_task(manager.unsubscribe_all_for(node_id, chain_id))
                self._store.remove_watch(node_id, chain_id)
                self._self_owned_addresses = self._store.all_addresses()
            return
        if etype == "peer.removed":
            node_id = str(event.get("node_id", "") or "")
            if node_id and node_id != "batch":
                for watch in self._store.list_watches():
                    if watch.get("node_id") != node_id or watch.get("status") != "watching":
                        continue
                    chain_id = str(watch.get("chain_id", "") or "")
                    self._signature_watch_requests.pop((node_id, chain_id), None)
                    manager = self._subscription_managers.get(chain_id)
                    if manager is not None:
                        self._queue_ws_task(manager.unsubscribe_all_for(node_id, chain_id))
                    self._store.remove_watch(node_id, chain_id)
                self._self_owned_addresses = self._store.all_addresses()
            elif node_id == "batch":
                # Bulk prune — cross-reference active watches against current peer table
                try:
                    current_peers = {p.node_id for p in (self._ctx.get_peers() or [])}
                except Exception:
                    current_peers = set()
                for watch in self._store.list_watches():
                    if watch.get("status") != "watching":
                        continue
                    wnode_id = str(watch.get("node_id", "") or "")
                    if wnode_id and wnode_id not in current_peers:
                        chain_id = str(watch.get("chain_id", "") or "")
                        self._signature_watch_requests.pop((wnode_id, chain_id), None)
                        manager = self._subscription_managers.get(chain_id)
                        if manager is not None:
                            self._queue_ws_task(manager.unsubscribe_all_for(wnode_id, chain_id))
                        self._store.remove_watch(wnode_id, chain_id)
                self._self_owned_addresses = self._store.all_addresses()
            return
        if etype == "bcw.poll":
            return

    def _drain_bus_events(self) -> None:
        if not self._sub:
            return
        poll = getattr(self._sub, "poll", None)
        if not callable(poll):
            return
        try:
            for event in poll() or []:
                if isinstance(event, dict):
                    self._handle_bus_event(event)
        except Exception as exc:
            self._log_warning("BCW bus poll failed: %s", exc)

    def _handle_watch_request(self, watch_request: Any, chain_id: Optional[str] = None) -> None:
        if not self._master_seed:
            return
        if isinstance(watch_request, dict):
            node_id = str(watch_request.get("node_id", "") or "")
            chain_id = str(watch_request.get("chain_id", "solana-mainnet") or "solana-mainnet")
            ttl_seconds = watch_request.get("ttl_seconds")
            correlation_id = watch_request.get("correlation_id")
            token_filter = watch_request.get("token_filter")
            tx_hash = str(watch_request.get("tx_hash", "") or "")
            requested_by = str(
                watch_request.get("requested_by")
                or watch_request.get("from_node")
                or node_id
            )
        else:
            node_id = str(watch_request or "")
            chain_id = str(chain_id or "solana-mainnet")
            ttl_seconds = None
            correlation_id = None
            token_filter = None
            tx_hash = ""
            requested_by = node_id
        if not node_id:
            return
        if chain_id not in self._solana_modules:
            self._log_warning("Unsupported chain in watch request: %s", chain_id)
            return
        try:
            address = derive_counterparty_address(self._master_seed, node_id, chain_id)
        except Exception as exc:
            self._log_warning("Invalid watch request node_id=%s: %s", node_id[:8], exc)
            return

        now = time.time()
        try:
            ttl_value = float(ttl_seconds) if ttl_seconds is not None else None
        except (TypeError, ValueError):
            ttl_value = None
        expires_at = now + ttl_value if ttl_value is not None else None
        correlation_text = None if correlation_id in (None, "") else str(correlation_id)
        token_filter_text = _serialize_watch_value(token_filter)

        self._store.upsert_watch(
            node_id,
            chain_id,
            address,
            token_filter=token_filter_text,
            requested_by=requested_by,
            correlation_id=correlation_text,
            created_at=now,
            expires_at=expires_at,
            status="watching",
        )
        self._self_owned_addresses = self._store.all_addresses()
        if tx_hash:
            self._signature_watch_requests[(node_id, chain_id)] = {
                "tx_hash": tx_hash,
                "correlation_id": correlation_text,
            }
        manager = self._subscription_managers.get(chain_id)
        if manager is not None:
            self._queue_ws_task(
                self._subscribe_watch(
                    node_id,
                    chain_id,
                    address,
                    correlation_text,
                    token_filter_text,
                    tx_hash or None,
                )
            )
        self._emit_event(
            "bcw.address_assigned",
            node_id=node_id,
            chain_id=chain_id,
            address=address,
            correlation_id=correlation_text,
        )

    async def _subscribe_watch(
        self,
        node_id: str,
        chain_id: str,
        address: str,
        correlation_id: Optional[str],
        token_filter: Any,
        tx_hash: Optional[str],
    ) -> None:
        watcher = self._solana_modules.get(chain_id)
        manager = self._subscription_managers.get(chain_id)
        if not watcher or not manager:
            return
        await manager.subscribe_account(address, node_id, correlation_id)
        for ata_address in self._register_ata_watches(node_id, chain_id, address, watcher, token_filter):
            await manager.subscribe_account(ata_address, node_id, correlation_id)
        if tx_hash:
            await manager.subscribe_signature(tx_hash, node_id, correlation_id)

    async def _expire_watch(self, entry: dict) -> None:
        node_id = str(entry.get("node_id", "") or "")
        chain_id = str(entry.get("chain_id", "") or "")
        if not node_id or not chain_id:
            return
        manager = self._subscription_managers.get(chain_id)
        if manager is not None:
            await manager.unsubscribe_all_for(node_id, chain_id)
        self._signature_watch_requests.pop((node_id, chain_id), None)
        activity_seen = self._store.activity_seen_since_watch(node_id, chain_id)
        self._store.update_status(node_id, chain_id, "expired")
        self._self_owned_addresses = self._store.all_addresses()
        self._emit_event(
            "bcw.watch.expired",
            node_id=node_id,
            chain_id=chain_id,
            address=entry.get("address"),
            activity_seen=activity_seen,
            correlation_id=entry.get("correlation_id"),
        )

    async def _poll_gap_recovery(self, chain_id: str) -> None:
        watcher = self._solana_modules.get(chain_id)
        if watcher is None:
            return
        now = time.time()
        if now - self._last_gap_recovery.get(chain_id, 0.0) < 30.0:
            return
        self._last_gap_recovery[chain_id] = now

        for item in self._store.list_watches():
            if item.get("chain_id") != chain_id or item.get("status") != "watching":
                continue
            try:
                result: PollResult = await watcher.poll_address(
                    item.get("address", ""),
                    item.get("last_signature"),
                )
            except Exception as exc:
                self._log_warning(
                    "BCW gap recovery failed chain=%s address=%s: %s",
                    chain_id,
                    item.get("address", "")[:12],
                    exc,
                )
                continue

            if result.latest_signature and result.latest_signature != item.get("last_signature"):
                self._store.update_cursor(item["node_id"], chain_id, result.latest_signature)
            if result.events:
                self._store.update_last_seen(item["node_id"], chain_id, now)

            for transfer in result.events:
                if not self._store.mark_seen(transfer.chain_id, transfer.tx_hash, transfer.tx_index):
                    continue
                self._process_transfer(transfer, correlation_id=item.get("correlation_id"))

    def _on_ws_notification(self, meta: dict, result: Any) -> None:
        node_id = str(meta.get("node_id", "") or "")
        chain_id = str(meta.get("chain_id", "") or "")
        correlation_id = meta.get("correlation_id")
        if node_id and chain_id:
            self._store.update_last_seen(node_id, chain_id, time.time())

        try:
            if meta.get("type") == "account":
                task = asyncio.get_running_loop().create_task(
                    self._process_ws_account_event(node_id, chain_id, correlation_id)
                )
                self._ws_tasks = [t for t in self._ws_tasks if not t.done()]
                self._ws_tasks.append(task)
            elif meta.get("type") == "signature":
                task = asyncio.get_running_loop().create_task(
                    self._process_ws_signature_event(
                        node_id, chain_id, meta.get("tx_hash"), correlation_id
                    )
                )
                self._ws_tasks = [t for t in self._ws_tasks if not t.done()]
                self._ws_tasks.append(task)
        except Exception as exc:
            self._log_warning("BCW websocket notification failed: %s", exc)

    async def _on_ws_reconnect(self, chain_id: str) -> None:
        await self._poll_gap_recovery(chain_id)
        for item in self._store.list_watches():
            if item.get("chain_id") != chain_id or item.get("status") != "watching":
                continue
            sig_watch = self._signature_watch_requests.get((item["node_id"], chain_id), {})
            await self._subscribe_watch(
                item["node_id"],
                chain_id,
                item.get("address", ""),
                item.get("correlation_id"),
                item.get("token_filter"),
                sig_watch.get("tx_hash"),
            )

    async def _process_ws_account_event(
        self,
        node_id: str,
        chain_id: str,
        correlation_id: Optional[str],
    ) -> None:
        """BCW-01: Fetch transfers via poll_address and route through _process_transfer."""
        # Look up watch entry for address and last_signature
        address = None
        last_signature = None
        for entry in self._store.list_watches():
            if entry.get("node_id") == node_id and entry.get("chain_id") == chain_id:
                address = entry.get("address")
                last_signature = entry.get("last_signature")
                break

        watcher = self._solana_modules.get(chain_id)
        if watcher is None or not address:
            return

        try:
            result: PollResult = await watcher.poll_address(address, last_signature)
        except Exception as exc:
            self._log_warning(
                "BCW01 ws account event poll failed node=%.16s chain=%s: %s",
                node_id, chain_id, exc,
            )
            return

        for transfer in result.events:
            if not self._store.mark_seen(transfer.chain_id, transfer.tx_hash, transfer.tx_index):
                continue
            self._process_transfer(transfer, correlation_id=correlation_id)

        if result.latest_signature and result.latest_signature != last_signature:
            self._store.update_cursor(node_id, chain_id, result.latest_signature)
        if result.events:
            self._store.update_last_seen(node_id, chain_id, time.time())

    async def _process_ws_signature_event(
        self,
        node_id: str,
        chain_id: str,
        tx_hash: Optional[str],
        correlation_id: Optional[str],
    ) -> None:
        """BCW-01: Fetch confirmed tx details via poll and route through _process_transfer."""
        watcher = self._solana_modules.get(chain_id)
        if watcher is None or not tx_hash:
            return

        address = self._store.get_address(node_id, chain_id)
        if not address:
            return

        try:
            result: PollResult = await watcher.poll_address(address, None)
        except Exception as exc:
            self._log_warning(
                "BCW01 ws signature event poll failed node=%.16s chain=%s tx=%.16s: %s",
                node_id, chain_id, tx_hash, exc,
            )
            return

        matched = False
        for transfer in result.events:
            if transfer.tx_hash != tx_hash:
                continue
            matched = True
            if not self._store.mark_seen(transfer.chain_id, transfer.tx_hash, transfer.tx_index):
                continue
            self._process_transfer(transfer, correlation_id=correlation_id)

        if not matched:
            self._log_warning(
                "BCW01 ws signature event: no matching tx found node=%.16s chain=%s tx=%.16s",
                node_id, chain_id, tx_hash,
            )

    # -- Superseded by BCW-01 async task approach (v0.49.0) --
    # These methods are kept as private stubs; no active call sites remain.

    def _process_account_notification(
        self,
        node_id: str,
        chain_id: str,
        result: dict,
        correlation_id: Optional[str],
    ) -> None:
        """Superseded by _process_ws_account_event (BCW-01). Retained as stub."""
        pass

    def _process_signature_notification(
        self,
        node_id: str,
        chain_id: str,
        result: dict,
        correlation_id: Optional[str],
    ) -> None:
        """Superseded by _process_ws_signature_event (BCW-01). Retained as stub."""
        pass

    async def on_tick(self, peers: list, health: NodeHealth) -> None:
        if not self._enabled:
            return
        if self._ws_available and not self._ws_started:
            self._start_ws_managers()
        self._drain_bus_events()
        for entry in self._store.get_expired_watches(time.time()):
            await self._expire_watch(entry)
        self._self_owned_addresses = self._store.all_addresses()
        for chain_id in self._solana_modules:
            manager = self._subscription_managers.get(chain_id)
            if manager is None or not manager._connected:
                await self._poll_gap_recovery(chain_id)
        await self._check_sol_balance()
        self._publish_meta_once()

    async def on_shutdown(self) -> None:
        for task in list(self._ws_tasks):
            task.cancel()
        for manager in self._subscription_managers.values():
            await manager.disconnect()
        return None

    def _publish_meta_once(self) -> None:
        if self._meta_published:
            return
        self._meta_published = True
        self._emit_event(
            "bcw.capabilities",
            chains=sorted(self._solana_modules.keys()),
            poll_only=not self._ws_available,
            poll_interval_seconds=self._poll_interval,
        )

    def _process_transfer(self, transfer: TransferEvent, correlation_id: Optional[str] = None) -> None:
        receipt_type = self._classify_transfer(transfer)
        if not receipt_type:
            return

        chain_topic = _chain_topic(transfer.chain_id)
        dedup_key = _dedup_key(transfer)
        common_fields = {
            "chain_id": transfer.chain_id,
            "tx_hash": transfer.tx_hash,
            "tx_index": transfer.tx_index,
            "from_address": transfer.from_address,
            "to_address": transfer.to_address,
            "amount": transfer.amount,
            "denom": transfer.denom,
            "decimals": transfer.decimals,
            "mint": transfer.mint_address,
            "dedup_key": dedup_key,
            "correlation_id": correlation_id,
        }

        if receipt_type == "payment_received":
            received_id = self._write_receipt(
                "payment_received",
                transfer,
                {"correlation_id": correlation_id},
            )
            self._emit_event(f"payment.received.{chain_topic}", receipt_type=receipt_type, **common_fields)
            if transfer.confirmation == ConfirmationStatus.FINALIZED:
                _finalized_is_new = self._store.latest_receipt_for(dedup_key, "payment_finalized") is None
                finalized_id = self._write_receipt(
                    "payment_finalized",
                    transfer,
                    {"original_receipt_id": received_id, "correlation_id": correlation_id},
                )
                if _finalized_is_new:
                    self._emit_event(
                        f"payment.finalized.{chain_topic}",
                        receipt_type="payment_finalized",
                        original_receipt_id=received_id,
                        receipt_id=finalized_id,
                        **common_fields,
                    )
            return

        if receipt_type == "payment_executed":
            receipt_id = self._write_receipt(
                "payment_executed",
                transfer,
                {"correlation_id": correlation_id},
            )
            self._emit_event(
                f"payment.sent.{chain_topic}",
                receipt_type=receipt_type,
                receipt_id=receipt_id,
                **common_fields,
            )
            if transfer.confirmation == ConfirmationStatus.FINALIZED:
                self._emit_event(
                    f"payment.sent.finalized.{chain_topic}",
                    receipt_type=receipt_type,
                    receipt_id=receipt_id,
                    **common_fields,
                )
            return

        if receipt_type == "wallet_transfer":
            receipt_id = self._write_receipt(
                "wallet_transfer",
                transfer,
                {"addresses": {"both_self_owned": True}, "correlation_id": correlation_id},
            )
            self._emit_event(
                f"payment.sent.{chain_topic}",
                receipt_type=receipt_type,
                receipt_id=receipt_id,
                **common_fields,
            )
            self._emit_event("wallet_transfer", receipt_id=receipt_id, **common_fields)
            return

        if receipt_type == "wallet_withdrawal":
            receipt_id = self._write_receipt(
                "wallet_withdrawal",
                transfer,
                {"counterparty": None, "correlation_id": correlation_id},
            )
            self._emit_event(
                f"payment.sent.{chain_topic}",
                receipt_type=receipt_type,
                receipt_id=receipt_id,
                **common_fields,
            )
            self._emit_event("wallet_withdrawal", receipt_id=receipt_id, **common_fields)

    def _handle_payment_finalized(self, event: dict) -> None:
        if not str(event.get("event", "")).startswith("payment.finalized."):
            return
        return

    def _economy_config(self) -> dict[str, Any]:
        # Fix #4: prefer global economy config from PluginContext over plugin-scoped config
        ctx_economy = getattr(self._ctx, "economy_config", None)
        if isinstance(ctx_economy, dict) and ctx_economy:
            return ctx_economy
        economy_cfg = self._config.get("economy", {})
        return economy_cfg if isinstance(economy_cfg, dict) else {}

    async def _check_sol_balance(self) -> None:
        now = time.time()
        if now - self._last_balance_check < self._balance_check_interval:
            return
        self._last_balance_check = now

        try:
            threshold = float(self._config.get("sol_min_balance", 0.01) or 0.01)
        except (TypeError, ValueError):
            threshold = 0.01

        for chain_id, watcher in self._solana_modules.items():
            if not self._master_seed:
                continue
            address = _derive_master_address(self._master_seed, chain_id)
            try:
                resp = await watcher._call_rpc(
                    "getBalance",
                    [address, {"commitment": watcher.commitment}],
                )
                lamports = int(((resp.get("result") or {}) if isinstance(resp, dict) else {}).get("value", 0) or 0)
            except Exception as exc:
                self._log_warning("BCW balance refresh failed for %s: %s", chain_id, exc)
                continue

            balance = lamports / _LAMPORTS_PER_SOL
            if balance < threshold:
                self._emit_event(
                    "wallet.sol_low",
                    chain_id=chain_id,
                    address=address,
                    balance=balance,
                    threshold=threshold,
                )
                self._log_warning(
                    "SOL balance below threshold chain=%s balance=%.9f threshold=%.9f",
                    chain_id,
                    balance,
                    threshold,
                )

    def _classify_transfer(self, transfer: TransferEvent) -> str:
        self_addresses = self._self_owned_addresses or self._store.all_addresses()
        from_self = transfer.from_address in self_addresses
        to_self = transfer.to_address in self_addresses

        if not from_self and to_self:
            return "payment_received"
        if from_self and to_self:
            return "wallet_transfer"
        if from_self and not to_self:
            return "wallet_withdrawal"
        if to_self:
            return "payment_received"
        return ""

    def _write_receipt(self, document_type: str, transfer: TransferEvent, extra: dict) -> str:
        dedup_key = _dedup_key(transfer)
        existing = self._store.latest_receipt_for(dedup_key, document_type)
        if existing:
            return existing

        receipt_id = f"{_PREFIX_BY_TYPE[document_type]}_{secrets.token_hex(8)}"
        body = {
            "document_type": document_type,
            "version": 1,
            "receipt_id": receipt_id,
            "timestamp": _iso_now(),
            "identity": self._ctx.node_id,
            "chain_id": transfer.chain_id,
            "tx_hash": transfer.tx_hash,
            "tx_index": transfer.tx_index,
            "from_address": transfer.from_address,
            "to_address": transfer.to_address,
            "amount": transfer.amount,
            "denom": transfer.denom,
            "decimals": transfer.decimals,
            "slot": transfer.slot,
            "block_time": transfer.block_time,
            "finality": {"level": transfer.confirmation.value},
            "dedup_key": dedup_key,
        }
        body.update(extra)

        signer = getattr(self._ctx, "sign_document", None)
        secured = body
        if callable(signer):
            try:
                signed = signer(body)
                if isinstance(signed, dict):
                    secured = signed
            except Exception as exc:
                self._log_warning("sign_document failed for %s: %s", document_type, exc)

        payload_json = json.dumps(secured, sort_keys=True, separators=(",", ":"), default=str)
        self._store.write_receipt(receipt_id, document_type, dedup_key, payload_json)
        return receipt_id

    def _emit_event(self, topic: str, **fields: Any) -> None:
        emitter = getattr(self._ctx, "emit_event", None)
        if callable(emitter):
            emitter(topic, **fields)

    def _log_warning(self, msg: str, *args: Any) -> None:
        logger = getattr(self._ctx, "log", None)
        if logger and hasattr(logger, "warning"):
            logger.warning(msg, *args)
        else:
            log.warning(msg, *args)

    def _knarr_mint(self) -> str:
        from knarr.core.constants import KNARR_MINT

        return str(self._config.get("knarr_mint") or KNARR_MINT or "")


BCWHandler = BCWPlugin


def _chain_topic(chain_id: str) -> str:
    if chain_id == "solana-mainnet":
        return "solana"
    if chain_id == "solana-devnet":
        return "solana_devnet"
    if chain_id == "solana-testnet":
        return "solana_testnet"
    return chain_id.replace("-", "_")


def _dedup_key(transfer: TransferEvent) -> str:
    return f"{transfer.chain_id}:{transfer.tx_hash}:{transfer.tx_index}"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
