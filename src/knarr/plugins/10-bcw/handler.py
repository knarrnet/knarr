import hashlib
import json
import logging
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from nacl.signing import SigningKey

from knarr.commerce.transfer_event import ConfirmationStatus, TransferEvent
from knarr.core.wallet import derive_solana_address
from knarr.dht.plugins import NodeHealth, PluginContext, PluginHooks

try:
    from .solana import PollResult, SolanaWatcher
except ImportError:  # PluginLoader injects sibling modules as top-level names
    from solana import PollResult, SolanaWatcher

log = logging.getLogger("knarr.plugin.bcw")

_PREFIX_BY_TYPE = {
    "payment_received": "prx",
    "payment_finalized": "pfin",
    "payment_executed": "pexe",
    "wallet_transfer": "wtfr",
    "wallet_withdrawal": "wwdr",
}


def derive_counterparty_address(master_seed: bytes, node_id: str, chain_id: str) -> str:
    if len(node_id) != 64:
        raise ValueError(f"node_id must be 64 hex chars, got {len(node_id)}")
    try:
        bytes.fromhex(node_id)
    except ValueError:
        raise ValueError(f"node_id must be valid hex, got {node_id[:16]}...")
    seed = hashlib.sha256(master_seed + node_id.encode("utf-8")).digest()
    if chain_id == "solana-mainnet":
        return derive_solana_address(SigningKey(seed))
    raise ValueError(f"Unsupported chain: {chain_id}")


def _derive_master_address(master_seed: bytes, chain_id: str) -> str:
    if chain_id == "solana-mainnet":
        return derive_solana_address(SigningKey(master_seed))
    raise ValueError(f"Unsupported chain: {chain_id}")


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

    def upsert_watch(self, node_id: str, chain_id: str, address: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bcw_watchlist (node_id, chain_id, address, last_signature)
                VALUES (?, ?, ?, NULL)
                ON CONFLICT(node_id, chain_id)
                DO UPDATE SET address=excluded.address
                """,
                (node_id, chain_id, address),
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
                SELECT node_id, chain_id, address, last_signature
                FROM bcw_watchlist
                ORDER BY chain_id, node_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def all_addresses(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT address FROM bcw_watchlist").fetchall()
        return {row["address"] for row in rows if row["address"]}

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
        self._known_wallet_addresses: set[str] = set()
        self._self_owned_addresses: set[str] = set()
        self._sub = None

        self._store = WatchStore(ctx.plugin_dir / "bcw.sqlite3")
        self._solana_modules: dict[str, SolanaWatcher] = {}
        for chain_cfg in self._config.get("chains", []):
            chain_id = str(chain_cfg.get("chain_id", ""))
            if chain_id == "solana-mainnet":
                self._solana_modules[chain_id] = SolanaWatcher(chain_id, chain_cfg)

        self._master_seed = self._load_master_seed()
        if self._enabled and not self._master_seed:
            self._enabled = False
            self._log_warning("BCW disabled: missing/invalid bcw_master_seed in vault")

        if self._enabled:
            self._bootstrap_watchlist()

        subscribe = getattr(ctx, "subscribe_events", None)
        if callable(subscribe):
            try:
                self._sub = subscribe("bcw.watch_request", "bcw.unwatch", "bcw.poll")
            except Exception as exc:
                self._log_warning("BCW event subscription failed: %s", exc)

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

    def _bootstrap_watchlist(self) -> None:
        if not self._master_seed:
            return

        for chain_id in self._solana_modules:
            self._store.upsert_watch("__master__", chain_id, _derive_master_address(self._master_seed, chain_id))
            self._store.upsert_watch(
                self._ctx.node_id,
                chain_id,
                derive_counterparty_address(self._master_seed, self._ctx.node_id, chain_id),
            )

        chain_ids = set(self._solana_modules.keys())
        for peer in self._safe_get_peers():
            self._maybe_add_peer_watch(peer, chain_ids)

        self._self_owned_addresses = self._store.all_addresses()

    def _safe_get_peers(self) -> list:
        get_peers = getattr(self._ctx, "get_peers", None)
        if callable(get_peers):
            try:
                peers = get_peers()
                return list(peers or [])
            except Exception:
                return []
        return []

    def _maybe_add_peer_watch(self, peer: Any, chain_ids: set[str]) -> None:
        if not self._master_seed:
            return
        node_id = str(getattr(peer, "node_id", "") or "")
        if len(node_id) == 64 and chain_ids:
            for chain_id in chain_ids:
                try:
                    address = derive_counterparty_address(self._master_seed, node_id, chain_id)
                    self._store.upsert_watch(node_id, chain_id, address)
                except Exception as exc:
                    self._log_warning("Failed deriving address for peer %s: %s", node_id[:8], exc)
        wallet = str(getattr(peer, "wallet", "") or "").strip()
        if wallet:
            self._known_wallet_addresses.add(wallet)

    def _handle_bus_event(self, event: dict) -> None:
        etype = event.get("event")
        if etype == "bcw.watch_request":
            node_id = str(event.get("node_id", "") or "")
            chain_id = str(event.get("chain_id", "solana-mainnet") or "solana-mainnet")
            self._handle_watch_request(node_id, chain_id)
            return
        if etype == "bcw.unwatch":
            node_id = str(event.get("node_id", "") or "")
            chain_id = str(event.get("chain_id", "solana-mainnet") or "solana-mainnet")
            if node_id:
                self._store.remove_watch(node_id, chain_id)
                self._self_owned_addresses = self._store.all_addresses()
            return
        if etype == "bcw.poll":
            # Poll is already happening inside this tick.
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

    def _handle_watch_request(self, node_id: str, chain_id: str) -> None:
        if not self._master_seed:
            return
        if chain_id not in self._solana_modules:
            self._log_warning("Unsupported chain in watch request: %s", chain_id)
            return
        try:
            address = derive_counterparty_address(self._master_seed, node_id, chain_id)
        except Exception as exc:
            self._log_warning("Invalid watch request node_id=%s: %s", node_id[:8], exc)
            return

        self._store.upsert_watch(node_id, chain_id, address)
        self._self_owned_addresses = self._store.all_addresses()
        self._emit_event("bcw.address_assigned", node_id=node_id, chain_id=chain_id, address=address)

    async def on_tick(self, peers: list, health: NodeHealth) -> None:
        if not self._enabled:
            return

        self._drain_bus_events()
        self._known_wallet_addresses = set()

        chain_ids = set(self._solana_modules.keys())
        for peer in peers or []:
            self._maybe_add_peer_watch(peer, chain_ids)

        watches = self._store.list_watches()
        self._self_owned_addresses = {w["address"] for w in watches if w.get("address")}
        rpc_ok = False

        for item in watches:
            chain_id = item.get("chain_id")
            watcher = self._solana_modules.get(chain_id)
            if not watcher:
                continue

            try:
                result: PollResult = await watcher.poll_address(
                    item.get("address", ""),
                    item.get("last_signature"),
                )
                rpc_ok = rpc_ok or bool(result.rpc_ok)
            except Exception as exc:
                self._log_warning(
                    "BCW poll failed chain=%s address=%s: %s",
                    chain_id,
                    item.get("address", "")[:12],
                    exc,
                )
                continue

            if result.latest_signature and result.latest_signature != item.get("last_signature"):
                self._store.update_cursor(item["node_id"], chain_id, result.latest_signature)

            for transfer in result.events:
                if not self._store.mark_seen(transfer.chain_id, transfer.tx_hash, transfer.tx_index):
                    continue
                self._process_transfer(transfer)

        if rpc_ok:
            self._publish_meta_once()

    async def on_shutdown(self) -> None:
        return None

    def _publish_meta_once(self) -> None:
        if self._meta_published:
            return
        self._meta_published = True
        self._emit_event(
            "bcw.capabilities",
            chains=sorted(self._solana_modules.keys()),
            poll_only=True,
            poll_interval_seconds=self._poll_interval,
        )

    def _process_transfer(self, transfer: TransferEvent) -> None:
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
            "dedup_key": dedup_key,
        }

        if receipt_type == "payment_received":
            received_id = self._write_receipt("payment_received", transfer, {})
            self._emit_event(f"payment.received.{chain_topic}", receipt_type=receipt_type, **common_fields)
            if transfer.confirmation == ConfirmationStatus.FINALIZED:
                finalized_id = self._write_receipt(
                    "payment_finalized",
                    transfer,
                    {"original_receipt_id": received_id},
                )
                self._emit_event(
                    f"payment.finalized.{chain_topic}",
                    receipt_type="payment_finalized",
                    original_receipt_id=received_id,
                    receipt_id=finalized_id,
                    **common_fields,
                )
            return

        if receipt_type == "payment_executed":
            receipt_id = self._write_receipt("payment_executed", transfer, {})
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
                {"addresses": {"both_self_owned": True}},
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
            receipt_id = self._write_receipt("wallet_withdrawal", transfer, {"counterparty": None})
            self._emit_event(
                f"payment.sent.{chain_topic}",
                receipt_type=receipt_type,
                receipt_id=receipt_id,
                **common_fields,
            )
            self._emit_event("wallet_withdrawal", receipt_id=receipt_id, **common_fields)

    def _classify_transfer(self, transfer: TransferEvent) -> str:
        self_addresses = self._self_owned_addresses or self._store.all_addresses()
        from_self = transfer.from_address in self_addresses
        to_self = transfer.to_address in self_addresses
        to_known = transfer.to_address in self._known_wallet_addresses

        if not from_self and to_self:
            return "payment_received"
        if from_self and to_self:
            return "wallet_transfer"
        if from_self and not to_self and to_known:
            return "payment_executed"
        if from_self and not to_self and not to_known:
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


def _chain_topic(chain_id: str) -> str:
    if chain_id.startswith("solana"):
        return "solana"
    return chain_id.replace("-", "_")


def _dedup_key(transfer: TransferEvent) -> str:
    return f"{transfer.chain_id}:{transfer.tx_hash}:{transfer.tx_index}"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
