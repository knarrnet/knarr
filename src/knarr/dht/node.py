import asyncio
import concurrent.futures
import hashlib
import json
import logging
import math
import os
import random
import secrets
import threading
import time
import uuid
import dataclasses
from datetime import datetime, timezone
from dataclasses import asdict, replace
from pathlib import Path
from typing import List, Optional, Dict, Any, Set, Callable
from nacl.signing import SigningKey, VerifyKey

from .. import __version__
from ..core.models import NodeInfo, SkillSheet, Task, Policy, GroupPolicy, SkillPolicy
from ..core.messages import (
    JoinRequest, JoinResponse, Announce, Query, QueryResponse,
    Deregister, Heartbeat, Ack, Message, TaskRequest, TaskStatus, TaskResult,
    SyncRequest, SyncResponse, Warn, Blocked, MailSync, MailAck, PluginMessage,
    MailPullReq, MailPullResp, MailPullAck,
    sign_message, verify_message, verify_node_id
)
from ..core.validation import validate_skill_sheet, validate_task_input, ValidationError
from ..core.pricing import PriceBreakdown, RealmConfig
from .storage import Storage
from .protocol import send_message, receive_message, request_response, ProtocolError
from .sidecar import AssetSidecar, TaskContext
from .pool import ConnectionPool
from .plugins import PluginLoader, NodeHealth
from .eventbus import EventBus

logger = logging.getLogger(__name__)

MAX_CONCURRENT_CONNECTIONS = 256
CONNECTION_TIMEOUT = 10.0  # seconds — client connect/send timeout
SERVER_IDLE_TIMEOUT = 120.0  # seconds — server keeps pooled connections open this long
MAX_ANNOUNCE_HOPS = 2
MAX_DEDUP_SET_SIZE = 10000
MAX_TASK_DEDUP_SIZE = 5000

HEARTBEAT_CHECK_INTERVAL = 10    # seconds between heartbeat scans
HEARTBEAT_SILENCE_THRESHOLD = 90  # seconds of silence before dedicated heartbeat
PEER_DEAD_TIMEOUT = 300          # seconds before removing silent peer
PEER_HEARTBEAT_SWEEP_TIMEOUT = 10.0
MIN_PEER_FLOOR = 8  # never prune below this many peers

def _parse_version(v: str) -> tuple:
    """Parse 'major.minor.patch' to tuple for comparison."""
    try:
        return tuple(int(p) for p in v.strip().split(".")[:3])
    except (ValueError, AttributeError):
        return (0, 0, 0)

class DHTNode:
    """A node in the Knarr DHT network with identity, propagation, task, and economy capabilities."""

    def __init__(self, host: str, port: int, storage_path: str = ":memory:",
                 advertise_host: Optional[str] = None, policy: Optional[Policy] = None,
                 config: Optional[Dict[str, Any]] = None, ephemeral: bool = False):
        self._main_loop = asyncio.get_event_loop()
        self.storage = Storage(storage_path)
        network_cfg = (config or {}).get("network", {})
        self._pool = ConnectionPool(max_connections=int(network_cfg.get("max_connections", 50)))
        self._connection_idle_timeout = float(network_cfg.get("connection_idle_timeout", 300))
        self._gossip_fanout = int(network_cfg.get("gossip_fanout", 3))
        self._bind_host = host
        economy_cfg = (config or {}).get("economy", {})
        _soft = float(economy_cfg.get("default_soft_limit", -5.0))
        _hard = float(economy_cfg.get("default_hard_limit", -10.0))
        if not (math.isfinite(_soft) and math.isfinite(_hard)):
            logger.warning(f"ECONOMY_CONFIG_INVALID soft={_soft} hard={_hard} — using defaults")
            _soft, _hard = -5.0, -10.0
        self.policy = policy or Policy(initial_credit=_soft, min_balance=_hard)
        self._config = config or {}
        self._ephemeral = ephemeral
        
        self._write_queue: asyncio.Queue = asyncio.Queue()
        self._write_queue_proto: asyncio.Queue = asyncio.Queue()  # priority: heartbeat, peer upserts
        self._start_time: float = 0.0
        
        self._sidecar: Optional[AssetSidecar] = None
        self._sidecar_port: int = 0
        self._asset_dir: str = ""
        self._generated_identity_certs = False

        self._signing_key: Optional[SigningKey] = None
        self._public_key_hex: str = ""
        self._x25519_private = None
        self._x25519_public = None
        self._encryption_key_hex: str = ""
        self._enc_debug: bool = self._config.get("encryption", {}).get("debug", False)
        # Egress filter initialization
        from ..core.egress_filter import EgressFilter
        self._egress = EgressFilter()

        self._vault: Optional['KeyringVault'] = None  # Set in _load_or_generate_node_id
        # C2: secrets managed by SecretsManager (must be created before _load_or_generate_node_id)
        from .secrets import SecretsManager
        self._secrets_mgr = SecretsManager()
        self._secrets: Dict[str, Dict[str, str]] = {}  # skill_name -> {key: value}
        node_id = self._load_or_generate_node_id()
        self._init_encryption()

        # Migrate TOML pricing discounts to SQL (one-time, v0.28.0)
        toml_discounts = self._config.get("pricing", {}).get("discounts", {})
        if toml_discounts:
            try:
                conn = self.storage._get_conn()
                existing = conn.execute("SELECT COUNT(*) FROM pricing_discounts").fetchone()[0]
                if existing == 0:
                    for group_name, pct_off in toml_discounts.items():
                        conn.execute("""
                            INSERT OR IGNORE INTO pricing_discounts (name, group_name, skill_group, effect_pct, priority, active, created_at)
                            VALUES (?, ?, '*', ?, 0, 1, ?)
                        """, (f"toml_{group_name}", group_name, float(pct_off), time.time()))
                    conn.commit()
                    logger.info(f"Migrated {len(toml_discounts)} TOML discount(s) to SQL")
            except Exception as e:
                logger.warning(f"TOML discount migration: {e}")
        
        effective_host = advertise_host if advertise_host else host
        self.node_info = NodeInfo(node_id=node_id, host=effective_host, port=port)
        
        self.server: Optional[asyncio.AbstractServer] = None
        self.background_tasks: List[asyncio.Task] = []
        self._running = False
        self._own_skills: Dict[str, SkillSheet] = {}
        self._skill_visibility: Dict[str, str] = {}  # skill_name -> "public"|"private"|"whitelist"
        self._skill_allowed_nodes: Dict[str, List[str]] = {}  # skill_name -> [node_id, ...]
        self._group_policies: List[GroupPolicy] = []  # from config, in config order
        self._group_engine = None  # GroupEngine, set in _init_group_engine()
        self._skill_policies: Dict[str, SkillPolicy] = {}  # skill_name -> SkillPolicy
        self._peer_last_activity: Dict[str, float] = {}
        self._peer_last_hb_work: Dict[str, float] = {}  # SA-FW2: throttle HB downstream work
        self._bootstrap_peers: List[str] = []  # stored on join() for re-bootstrap
        self._heartbeat_silence_threshold = float(network_cfg.get("heartbeat_silence_threshold", HEARTBEAT_SILENCE_THRESHOLD))
        self._peer_dead_timeout = float(network_cfg.get("peer_dead_timeout", PEER_DEAD_TIMEOUT))
        self._version_gated: bool = False  # True = below min_protocol_version, skills suspended
        self._min_protocol_version: str = self._config.get("node", {}).get("min_protocol_version", "")
        self._seen_messages: Set[tuple] = set()
        self._seen_task_requests: Set[str] = set()  # SA6-02: msg_id dedup for TaskRequests

        self._task_slots = max(1, min(64, int(self._config.get("node", {}).get("task_slots", 4))))
        # v0.33.0 C-track: configurable max queue depth (floor at 1 to prevent unbounded queue)
        _max_queue = max(1, int(self._config.get("node", {}).get("max_queue_depth", 100)))
        self._task_queue: asyncio.Queue = asyncio.Queue(maxsize=_max_queue)
        self._task_semaphore = asyncio.Semaphore(self._task_slots)
        self._active_workers = 0

        self._handlers: Dict[str, tuple[Callable, bool]] = {} # skill_name -> (handler_fn, slow)
        self._handler_specs: Dict[str, str] = {}   # skill_name -> handler spec string
        self._handler_mtimes: Dict[str, float] = {} # skill_name -> handler file mtime
        self._skill_active: Dict[str, int] = {}     # v0.37.0: per-skill active execution count
        self._task_events: Dict[str, asyncio.Event] = {}
        self._task_results: Dict[str, TaskResult] = {}
        self._task_expected_provider: Dict[str, str] = {}  # task_id -> expected provider public_key
        self._admission_cache: Dict[str, Dict[str, Any]] = {}  # job_id -> admission result

        self._mcp_bridges: List[Any] = []
        self._active_connections: int = 0  # SA-02: accept-level connection tracking
        # C2: _secrets_mgr created earlier (before _load_or_generate_node_id)
        self._upgrading: bool = False
        self._restart_requested: bool = False
        self._shutdown_event: Optional[asyncio.Event] = None  # set by main.py for clean upgrade restart
        self._notified_version: Optional[str] = getattr(self, "_notified_version", None)
        
        self._handler_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(32, (os.cpu_count() or 4) + 4)
        )


        # Meta cache registry and TTL (must be before register_meta_realm calls)
        self._meta_realms: dict = {}  # realm_name -> RealmConfig
        self._meta_ttl = {
            "node": 3600,
            "skill": 600,
            "self": 300,
            "group": 1800,
        }

        # v0.17.0: Mail v2 Sync Engine (before plugins so mail callbacks are available)
        from ..mail.sync import SyncEngine
        from .mail_handlers import MailHandlers
        self._sync = SyncEngine(self, plugins=None)  # plugins wired after PluginLoader init
        self._mail_handlers = MailHandlers(self.storage, None, self._asset_dir, self._sidecar)
        self._mail_handlers.bind_runtime(
            enqueue_write=self._enqueue_write,
            get_initial_trust=self._get_initial_trust,
            check_credit_restored=self._check_credit_restored,
            store_asset_cb=self.store_asset,
            signing_key=self._signing_key,
            public_key_hex=self._public_key_hex,
            initial_credit=self.policy.initial_credit,
            debug=bool(self._config.get("mail", {}).get("debug", False)),
            handler_pool=self._handler_pool,
        )

        # Register core realms
        self.register_meta_realm("node", RealmConfig(
            queries=["info"],
            access={"info": "public"}
        ))
        self.register_meta_realm("skill", RealmConfig(
            queries=["info"],
            access={"info": "public"}
        ))

        # Populate initial cache
        self._update_meta_cache("node", "info", {
            "node_id": self.node_info.node_id,
            "version": __version__,
            "jurisdiction": self._node_jurisdiction_wire,
            "skills_count": len(self._own_skills),
            "uptime": 0,
        })
        for sname, sheet in self._own_skills.items():
            self._update_meta_cache("skill", sname, {
                "name": sname,
                "price": sheet.price,
                "tags": getattr(sheet, 'tags', []),
                "version": getattr(sheet, 'version', ''),
            })
        self._sync.register_handler("knarr/system/task_result", self._mail_handlers._handle_task_result_mail)
        self._sync.register_handler("knarr/system/task_failed", self._mail_handlers._handle_task_failed_mail)
        self._sync.register_handler("knarr/system/asset_fetch", self._mail_handlers._handle_asset_fetch_mail)
        self._sync.register_handler("knarr/system/asset_ready", self._mail_handlers._handle_asset_ready_mail)

        # v0.25.0 Commerce handlers (async closures via factory)
        from ..commerce.handlers import make_commerce_handlers
        for msg_type, handler in make_commerce_handlers(self).items():
            self._sync.register_handler(msg_type, handler)

        # v0.32.0: EventBus — intra-node event channel (ring buffer, volatile)
        # Must be created before PluginLoader so bus callbacks can be wired into context.
        # v0.33.0: bus size configurable via [node] event_bus_size
        _bus_debug = bool(self._config.get("node", {}).get("event_bus_debug", False))
        _bus_size = int(self._config.get("node", {}).get("event_bus_size", 256))
        self.bus = EventBus(size=_bus_size, debug=_bus_debug)
        self._mail_handlers.bind_runtime(bus=self.bus)

        # V015: Plugin system
        config_dir = Path(self._config.get("_config_dir", os.getcwd()))
        data_dir = None
        if self._config.get("_data_dir_explicit"):
            data_dir = Path(self._config.get("_data_dir", config_dir))
        self._plugins = PluginLoader(
            config_dir=config_dir,
            get_peers_cb=lambda: self.storage.get_peers(),
            send_to_peer_cb=self._send_to_peer_raw,
            node_id=self.node_info.node_id,
            delivery_cb=self._process_message_callback,
            send_fire_forget_cb=self._send_fire_forget,
            register_mail_handler_cb=self._sync.register_handler,
            send_mail_cb=self._sync.enqueue,
            register_egress_material_cb=self._egress.register_sensitive_material if hasattr(self, '_egress') else None,
            vault_get_cb=self._vault.get if hasattr(self, '_vault') and self._vault else None,
            vault_set_cb=self._vault.set if hasattr(self, '_vault') and self._vault else None,
            storage_path=self.storage.db_path if hasattr(self.storage, 'db_path') else None,
            update_cache_cb=self._update_meta_cache,
            subscribe_events_cb=self.bus.subscribe,   # v0.32.0
            emit_event_cb=self.bus.emit,              # v0.32.0
            bus=self.bus,                             # v0.33.0: EventBus for plugins
            data_dir=data_dir,
        )

        internal_signer_keys: Dict[str, bytes] = {}
        thrall_path = os.path.join(
            str(data_dir or config_dir),
            "plugin_state",
            "06-thrall",
            "thrall_identity.key",
        )
        if os.path.exists(thrall_path):
            try:
                with open(thrall_path, "rb") as handle:
                    thrall_signing_key = SigningKey(handle.read())
                internal_signer_keys["thrall-1"] = thrall_signing_key.verify_key.encode()
                self._thrall_signing_key = thrall_signing_key
            except Exception as exc:
                logger.warning(f"Failed to load thrall identity key from {thrall_path}: {exc}")

        cockpit_signing_key = getattr(self, "_cockpit_signing_key", None)
        if cockpit_signing_key is not None:
            try:
                internal_signer_keys["cockpit-1"] = cockpit_signing_key.verify_key.encode()
            except Exception as exc:
                logger.warning(f"Failed to register cockpit signing key: {exc}")

        wm_cfg = dict(self._config.get("warehouse_manager", {}) or {})
        if wm_cfg.get("enabled", False):
            from ..core.warehouse_manager import WarehouseManager

            identity_fragments = [
                self.node_info.node_id,
                self._public_key_hex,
                f"did:knarr:{self.node_info.node_id}",
                f"did:knarr:{self.node_info.node_id}#key-1",
                f"did:knarr:{self.node_info.node_id}#cockpit-1",
                f"did:knarr:{self.node_info.node_id}#thrall-1",
            ]
            rules_cfg = dict(wm_cfg.get("rules", {}) or {})
            for settlement_type in (
                "settlement_prepared",
                "settlement_accepted",
                "settlement_processed",
                "settlement_confirmation",
            ):
                rules_cfg.setdefault(
                    settlement_type,
                    {"gates": [1, 2, 3, 4, 5], "action": "auto_promote"},
                )
            wm_cfg["rules"] = rules_cfg
            self.wm = WarehouseManager(
                node_id=self.node_info.node_id,
                identity_fragments=identity_fragments,
                internal_signer_keys=internal_signer_keys,
                bus=self.bus,
                storage=self.storage,
                config=wm_cfg,
                write_receipt_cb=self._write_receipt,
            )
        else:
            self.wm = None

        # M-016: Wire plugins into SyncEngine for on_mail_received hook
        self._sync._plugins = self._plugins

        # v0.17.4: Peer address overrides (same-LAN, NAT hairpin workaround)
        self._peer_overrides: Dict[str, tuple] = {}
        for nid, addr in self._config.get("peer_overrides", {}).items():
            try:
                h, p = addr.rsplit(":", 1)
                self._peer_overrides[nid] = (h, int(p))
                logger.info(f"PEER_OVERRIDE {nid[:16]} -> {h}:{p}")
            except (ValueError, TypeError):
                logger.warning(f"Invalid peer_override for {nid}: {addr!r} (expected 'host:port')")

    async def _wait_either_queue(self):
        """Wait for an item from either write queue. Protocol queue checked first."""
        while True:
            try:
                return self._write_queue_proto.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                return self._write_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            await asyncio.sleep(0.01)

    async def _writer_loop(self):
        """Batched writer: collects writes within a time window for efficiency.
        Protocol writes (heartbeats, peer upserts) drain before application writes.
        Yields to event loop every 10 items to prevent starvation."""
        BATCH_WINDOW_MS = 50
        BATCH_MAX_SIZE = 100
        while self._running:
            batch = []

            # Priority: drain ALL protocol writes first
            while not self._write_queue_proto.empty() and len(batch) < BATCH_MAX_SIZE:
                try:
                    item = self._write_queue_proto.get_nowait()
                    batch.append(item)
                except asyncio.QueueEmpty:
                    break

            # Then drain application writes if room remains
            if not batch:
                # Wait for first item from either queue
                try:
                    item = await asyncio.wait_for(self._wait_either_queue(), timeout=1.0)
                    batch.append(item)
                except asyncio.TimeoutError:
                    continue

            # Drain remaining app writes up to deadline or max size
            deadline = time.monotonic() + BATCH_WINDOW_MS / 1000
            while time.monotonic() < deadline and len(batch) < BATCH_MAX_SIZE:
                # Check proto queue first
                try:
                    item = self._write_queue_proto.get_nowait()
                    batch.append(item)
                    continue
                except asyncio.QueueEmpty:
                    pass
                try:
                    item = self._write_queue.get_nowait()
                    batch.append(item)
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.005)
                    try:
                        item = self._write_queue.get_nowait()
                        batch.append(item)
                    except asyncio.QueueEmpty:
                        break

            # Execute writes with periodic yield
            for i, (op, args, future) in enumerate(batch):
                try:
                    result = op(*args)
                    if not future.done():
                        future.set_result(result)
                except Exception as e:
                    if not future.done():
                        future.set_exception(e)
                if i % 10 == 9:
                    await asyncio.sleep(0)

    async def _enqueue_write_proto(self, op: Callable, *args: Any) -> Any:
        """Enqueue a protocol-priority write. Flushes before application writes."""
        current_loop = asyncio.get_running_loop()
        future = current_loop.create_future()
        await self._write_queue_proto.put((op, args, future))
        return await future

    async def _enqueue_write(self, op: Callable, *args: Any) -> Any:
        """Enqueue a write operation and wait for result. Loop-safe. [R-01]"""
        current_loop = asyncio.get_running_loop()
        
        if current_loop is self._main_loop:
            future = current_loop.create_future()
            self._write_queue.put_nowait((op, args, future))
            return await future
        else:
            # Cross-loop call: use run_coroutine_threadsafe bridge
            import concurrent.futures
            future = concurrent.futures.Future()
            def _bridge():
                main_fut = self._main_loop.create_future()
                self._write_queue.put_nowait((op, args, main_fut))
                def _done(f):
                    try:
                        future.set_result(f.result())
                    except Exception as e:
                        future.set_exception(e)
                main_fut.add_done_callback(_done)
            
            self._main_loop.call_soon_threadsafe(_bridge)
            return await asyncio.wrap_future(future)

    async def _task_worker_loop(self, worker_id: int):
        """Drains the task queue and executes handlers with semaphore control."""
        while self._running:
            try:
                work_item = await asyncio.wait_for(self._task_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            msg, handler_fn, slow, input_size, start_time, result_future = work_item
            # V013-008: Transition to 'running' before execution
            job_id = msg.input_data.get("_job_id") or msg.task_id
            await self._enqueue_write(self.storage.update_async_job_status, job_id, "running")
            # B3: task.started event
            if self.bus:
                caller_nid = hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest()
                self.bus.emit("task.started",
                    skill_name=msg.skill_name, caller_node=caller_nid,
                    task_id=job_id, identity=caller_nid,
                    queue_wait_ms=int((time.time() - start_time) * 1000))
            # B4: order_executing receipt — marks transition from queued to running
            _caller_nid_oexe = hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest()
            self._write_receipt(
                document_type="order_executing",
                payload={
                    "provider": self.node_info.node_id,
                    "caller": _caller_nid_oexe,
                    "skill_uri": f"knarr:///{msg.skill_name.lower()}",
                    "queue_wait_ms": int((time.time() - start_time) * 1000),
                },
                order_ref=job_id,
                proof_purpose="assertion",
                sign=False,
            )
            async with self._task_semaphore:  # Wait for available slot
                self._active_workers += 1
                try:
                    await self._execute_queued_task(msg, handler_fn, slow, input_size, start_time, result_future)
                finally:
                    self._active_workers -= 1

    async def _execute_queued_task(self, msg: TaskRequest, handler_fn: Callable,
                                    slow: bool, input_size: int, start_time: float,
                                    result_future: asyncio.Future):
        """Executes a task from the queue and resolves the result future."""
        from .mcp_bridge import MCPTimeoutError
        skill_name = msg.skill_name.lower()
        skill_sheet = self._own_skills.get(skill_name)

        # Pop cached admission result (price, breakdown, prepaid decision)
        job_id_lookup = msg.input_data.get("_job_id") or msg.task_id
        admission_meta = self._admission_cache.pop(job_id_lookup, None)
        prepaid_action = "skip"
        prepaid_amount = 0.0
        if admission_meta:
            skill_price = float(admission_meta.get("price", skill_sheet.price if skill_sheet else 1.0))
            prepaid_action = str(admission_meta.get("prepaid_action", "skip"))
            prepaid_amount = float(admission_meta.get("prepaid_amount", 0.0) or 0.0)
            breakdown_dict = admission_meta.get("breakdown") or {}
            from ..core.pricing import PriceBreakdown
            price_breakdown = PriceBreakdown(**breakdown_dict)
        else:
            skill_price = skill_sheet.price if skill_sheet else 1.0
            caller_nid = hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest()
            skill_price, price_breakdown = self._resolve_price(caller_nid, skill_price, skill_name)
        skill_cfg = self._get_skill_runtime_config(skill_name) or {}

        # H21-prep: Job ID propagation
        # For async jobs, msg.task_id is the generated job_id (sent in TaskStatus)
        job_id = msg.input_data.get("_job_id") or msg.task_id

        # v0.33.0 C-track: configurable default timeout
        _default_timeout_s = float(self._config.get("skills", {}).get("default_timeout", 30))
        max_timeout = self._config.get("node", {}).get("max_task_timeout", 3600)
        _req_timeout_s = msg.timeout_ms / 1000.0 if msg.timeout_ms else _default_timeout_s
        if max_timeout > 0:
            handler_timeout = min(_req_timeout_s, max_timeout)
        else:
            handler_timeout = _req_timeout_s

        input_hash = None  # M-6: ensure defined for exception handlers
        try:
            loop = asyncio.get_running_loop()

            # H21-prep: Job ID propagation
            job_id = msg.input_data.get("_job_id") or msg.task_id
            
            # Auto-resolve knarr-asset:// URIs in input_data (top-level only)
            input_data = msg.input_data
            input_data["_job_id"] = job_id
            
            asset_hash = None
            if self._asset_dir:
                resolved_input = dict(msg.input_data)
                for key, value in resolved_input.items():
                    if isinstance(value, str) and value.startswith("knarr-asset://"):
                        asset_hash_val = value[len("knarr-asset://"):]
                        # P8A1-001: Validate hash to prevent path traversal
                        if len(asset_hash_val) != 64 or not all(c in '0123456789abcdef' for c in asset_hash_val):
                            continue
                        if asset_hash is None:
                            asset_hash = asset_hash_val
                        asset_path = os.path.join(self._asset_dir, asset_hash_val)
                        if os.path.exists(asset_path):
                            resolved_input[key] = asset_path
                input_data = resolved_input

            # G-9: Inject per-skill secrets (caller values take precedence)
            input_data = self._inject_secrets(skill_name, input_data)

            # Inject caller identity for handlers that need it (e.g. knarr-mail local-only check)
            caller_node_id = hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest()
            input_data = dict(input_data)
            input_data["_caller_node_id"] = caller_node_id
            input_data["_node_encrypt"] = self.encrypt_for_peer
            input_data["_node_decrypt"] = self.decrypt_from_peer
            input_data["_send_mail"] = self._sync.enqueue

            # Compute input hash for log
            canonical = json.dumps(msg.input_data, sort_keys=True, separators=(',', ':'))
            input_hash = hashlib.sha256(canonical.encode()).hexdigest()

            # Check if handler accepts TaskContext
            import inspect
            ctx = TaskContext(self._asset_dir) if self._asset_dir else None
            handler_accepts_ctx = False
            try:
                sig = inspect.signature(handler_fn)
                handler_accepts_ctx = len(sig.parameters) >= 2
            except (ValueError, TypeError):
                pass

            if asyncio.iscoroutinefunction(handler_fn):
                # Async handler: run in thread pool with a thread-local event loop
                # so blocking work inside (regex, HTTP, subprocess) can't starve the main loop
                def _run_async():
                    tloop = asyncio.new_event_loop()
                    try:
                        if handler_accepts_ctx:
                            return tloop.run_until_complete(handler_fn(input_data, ctx))
                        else:
                            return tloop.run_until_complete(handler_fn(input_data))
                    finally:
                        tloop.close()
                logger.debug(f"Handler start: skill={skill_name} task={msg.task_id[:8]} async=True")
                result_data = await asyncio.wait_for(
                    loop.run_in_executor(self._handler_pool, _run_async),
                    timeout=handler_timeout)
            else:
                # Sync handler: run in thread pool
                logger.debug(f"Handler start: skill={skill_name} task={msg.task_id[:8]} async=False")
                if handler_accepts_ctx:
                    result_data = await asyncio.wait_for(
                        loop.run_in_executor(self._handler_pool, handler_fn, input_data, ctx),
                        timeout=handler_timeout
                    )
                else:
                    result_data = await asyncio.wait_for(
                        loop.run_in_executor(self._handler_pool, handler_fn, input_data),
                        timeout=handler_timeout
                    )

            wall_ms = int((time.time() - start_time) * 1000)
            logger.info(f"Task completed: skill={skill_name} task={msg.task_id[:8]} wall={wall_ms}ms")

            # Strip non-serializable injected hooks from result
            # (handlers may echo input_data which contains _node_encrypt/_node_decrypt methods)
            if isinstance(result_data, dict):
                result_data = {k: v for k, v in result_data.items() if not callable(v)}

            # Egress check on skill output BEFORE any storage or mail
            # (must run before async job update, task status, receipts, and mail enqueue)
            if result_data and hasattr(self, '_egress'):
                out_str = json.dumps(result_data) if isinstance(result_data, dict) else str(result_data)
                if not self._egress.check(out_str):
                    logger.critical(f"EGRESS_BLOCK_RESULT task={msg.task_id[:16]} skill={skill_name}")
                    # v0.33.0: security.egress_blocked
                    if self.bus:
                        self.bus.emit("security.egress_blocked", skill_name=skill_name, target=caller_node_id, identity=caller_node_id)
                    result_data = {"error": "SECURITY_VIOLATION", "code": "EGRESS_FILTER_BLOCKED"}

            # Use job_id for updates (propagated from Task object)
            job_id_for_update = input_data.get("_job_id") or msg.task_id

            await self._enqueue_write(
                self.storage.update_task_status, job_id_for_update, "completed",
                result_data, None, input_size, wall_ms
            )
            await self._enqueue_write(
                self.storage.log_execution,
                job_id_for_update, skill_name, caller_node_id, "completed", wall_ms, input_hash, asset_hash, None, skill_price, price_breakdown.to_json()
            )

            # v0.33.0: task.completed
            if self.bus:
                self.bus.emit("task.completed", skill_name=skill_name, caller_node=caller_node_id, task_id=job_id_for_update, wall_ms=wall_ms, price=skill_price, identity=caller_node_id)

            # E5: Check _billable flag — skip receipt + ledger if handler says not billable
            billable = True
            if isinstance(result_data, dict) and result_data.get("_billable") is False:
                billable = False
                logger.info(f"TASK_NOT_BILLABLE task={job_id_for_update[:8]} skill={skill_name}")

            receipt_json = None
            credit_note_json = None
            _note_type = "zero" if skill_price == 0 else "debit"
            if billable:
                # v0.23.0: Execution receipts (old format, backward compat)
                output_hash = hashlib.sha256(
                    json.dumps(result_data, sort_keys=True, separators=(',', ':')).encode()
                ).hexdigest() if result_data else ""
                receipt_json = self._sign_receipt(
                    task_id=job_id_for_update, skill_name=skill_name,
                    consumer_node_id=caller_node_id, credits_charged=skill_price,
                    input_hash=input_hash, output_hash=output_hash, wall_ms=wall_ms,
                    price_breakdown_json=price_breakdown.to_json()
                )
                await self._enqueue_write(self.storage.store_receipt, job_id_for_update, receipt_json)

                # B4: execution_receipt (success) in receipt_log
                from datetime import datetime, timezone as _tz_exec
                _completed_at = datetime.now(_tz_exec.utc)
                _completed_iso = _completed_at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_completed_at.microsecond // 1000:03d}Z"
                _started_at = _completed_at - __import__("datetime").timedelta(milliseconds=max(0, min(wall_ms, 86400000)))
                _started_iso = _started_at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_started_at.microsecond // 1000:03d}Z"
                self._write_receipt(
                    document_type="execution_receipt",
                    payload={
                        "provider": self.node_info.node_id,
                        "caller": caller_node_id,
                        "skill_uri": f"knarr:///{skill_name}",
                        "order_ref": job_id_for_update,
                        "execution": {
                            "status": "completed",
                            "started_at": _started_iso,
                            "completed_at": _completed_iso,
                            "duration_ms": wall_ms,
                            "input_hash": f"sha256:{input_hash}" if input_hash else None,
                            "output_hash": f"sha256:{output_hash}" if output_hash else None,
                            "error": None,
                        },
                        "settlement": {
                            "credit_note_ref": None,
                            "amount": float(skill_price),
                            "currency": "credits",
                        },
                    },
                    counterparty=caller_node_id,
                    order_ref=job_id_for_update,
                    proof_purpose="assertion",
                    sign=True,
                )

                # v0.32.0: Credit note (new format) — receipt before bus
                try:
                    from ..commerce.receipts import create_credit_note as _create_credit_note
                    caller_pubkey = getattr(msg, "public_key", "") or ""
                    credit_note_json = _create_credit_note(
                        note_type=_note_type,
                        amount=float(skill_price),
                        issuer=self._public_key_hex,
                        recipient=caller_pubkey,
                        reference=job_id_for_update,
                        description=f"skill:{skill_name} execution",
                        signing_key=self._signing_key,
                    )
                    # Store issuer's copy before firing event (design principle: receipt before bus)
                    await self._enqueue_write(
                        self.storage.store_credit_note,
                        caller_pubkey, job_id_for_update, credit_note_json
                    )
                    # B4: credit_note receipt in receipt_log
                    self._write_receipt(
                        document_type="credit_note",
                        payload={
                            "note_type": _note_type,
                            "amount": float(skill_price),
                            "currency": "credits",
                            "issuer": self.node_info.node_id,
                            "recipient": caller_pubkey,
                            "reference": job_id_for_update,
                            "description": f"skill:{skill_name} execution",
                        },
                        counterparty=caller_node_id,
                        order_ref=job_id_for_update,
                        proof_purpose="assertion",
                        sign=True,
                    )
                    # E3: receipt.issued fires AFTER storage
                    self.bus.emit(
                        "receipt.issued",
                        note_type=_note_type,
                        counterparty=caller_node_id,
                        amount=skill_price,
                        reference=job_id_for_update,
                        identity=caller_node_id,
                    )
                except Exception as _cn_err:
                    logger.warning(f"CREDIT_NOTE_ISSUE_FAIL job={job_id_for_update[:8]}: {_cn_err}")

            # H19: Update async job if exists
            is_async = getattr(msg, "mode", "sync") == "async"
            if is_async:
                await self._enqueue_write(self.storage.update_async_job_status, job_id_for_update, "completed", result_data)
                # v0.29.1: Skip mail roundtrip for self-calls — update directly
                if caller_node_id == self.node_info.node_id:
                    logger.debug(f"SELF_DELIVERY_SKIP job={job_id_for_update[:8]} — provider==consumer")
                else:
                    # v0.17.0: Push result via core Mail Sync
                    # v0.32.0: Embed credit_note field alongside legacy receipt field
                    try:
                        await self._sync.enqueue(
                            to_node=caller_node_id,
                            msg_type="knarr/system/task_result",
                            body={
                                "job_id": job_id_for_update,
                                "skill": skill_name,
                                "status": "completed",
                                "output_data": result_data,
                                "receipt": receipt_json,       # old format, backward compat
                                "credit_note": credit_note_json,  # new format v0.32.0
                            },
                            system=True,
                            ttl_hours=24,
                        )
                    except Exception as mail_err:
                        logger.warning(f"Async result mail enqueue failed for {job_id_for_update}: {mail_err}")

            if billable:
                # F1: Increment meter after successful execution
                caller_node_id = hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest()
                await self._enqueue_write(self.storage.meter_increment, caller_node_id, skill_name, "", 0)

                await self._apply_provider_billing(
                    msg=msg,
                    job_id_for_update=job_id_for_update,
                    skill_name=skill_name,
                    skill_price=skill_price,
                    skill_cfg=skill_cfg,
                    prepaid_action=prepaid_action,
                    prepaid_amount=prepaid_amount,
                )
            result_msg = self._sign(TaskResult(task_id=msg.task_id, status="completed", output_data=result_data, receipt=receipt_json))
            # M-016: Notify plugins of task completion
            asyncio.create_task(self._plugins.on_task_complete(
                skill_name, job_id_for_update, caller_node_id, result_data, wall_ms))

        except asyncio.TimeoutError:
            err = {"code": "TIMEOUT", "message": f"Handler exceeded {handler_timeout}s timeout"}
            # Set cancellation event for cooperative handlers
            if ctx:
                ctx.cancelled.set()
            # Log warning about orphaned thread
            if not asyncio.iscoroutinefunction(handler_fn):
                logger.warning(
                    f"ORPHAN_HANDLER skill={skill_name} job={job_id_for_update[:8]} "
                    f"timeout={handler_timeout}s — thread may still be running"
                )
            wall_ms = int((time.time() - start_time) * 1000)
            job_id_for_update = input_data.get("_job_id") or msg.task_id
            await self._enqueue_write(self.storage.update_task_status, job_id_for_update, "failed", None, err, input_size, wall_ms)
            await self._enqueue_write(
                self.storage.log_execution,
                job_id_for_update, skill_name, caller_node_id, "failed", wall_ms, input_hash, asset_hash, err["message"]
            )
            # v0.33.0: task.failed
            if self.bus:
                self.bus.emit("task.failed", skill_name=skill_name, caller_node=caller_node_id, task_id=job_id_for_update, error_type="TIMEOUT", identity=caller_node_id)
            is_async = getattr(msg, "mode", "sync") == "async"
            if is_async:
                await self._enqueue_write(self.storage.update_async_job_status, job_id_for_update, "failed", None, err)
                try:
                    await self._sync.enqueue(
                        to_node=caller_node_id,
                        msg_type="knarr/system/task_failed",
                        body={"job_id": job_id_for_update, "skill": skill_name, "error": err},
                        system=True,
                        ttl_hours=24,
                    )
                except Exception as mail_err:
                    logger.warning(f"Async error mail enqueue failed for {job_id_for_update}: {mail_err}")
            # B4: execution_receipt (failed — TimeoutError) in receipt_log
            from datetime import datetime, timezone as _tz_fail1
            _fail1_now = datetime.now(_tz_fail1.utc)
            _fail1_iso = _fail1_now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_fail1_now.microsecond // 1000:03d}Z"
            _fail1_start = _fail1_now - __import__("datetime").timedelta(milliseconds=max(0, min(wall_ms, 86400000)))
            _fail1_start_iso = _fail1_start.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_fail1_start.microsecond // 1000:03d}Z"
            self._write_receipt(
                document_type="execution_receipt",
                payload={
                    "provider": self.node_info.node_id,
                    "caller": caller_node_id,
                    "skill_uri": f"knarr:///{skill_name}",
                    "order_ref": job_id_for_update,
                    "execution": {
                        "status": "failed",
                        "started_at": _fail1_start_iso,
                        "completed_at": _fail1_iso,
                        "duration_ms": wall_ms,
                        "input_hash": f"sha256:{input_hash}" if input_hash else None,
                        "output_hash": None,
                        "error": err.get("message", "timeout"),
                    },
                    "settlement": {"credit_note_ref": None, "amount": 0.0, "currency": "credits"},
                },
                counterparty=caller_node_id,
                order_ref=job_id_for_update,
                proof_purpose="assertion",
                sign=True,
            )
            result_msg = self._sign(TaskResult(task_id=msg.task_id, status="failed", error=err))
            asyncio.create_task(self._plugins.on_task_complete(
                skill_name, job_id_for_update, caller_node_id, None, wall_ms))

        except MCPTimeoutError as e:
            err = {"code": "TIMEOUT", "message": str(e)}
            wall_ms = int((time.time() - start_time) * 1000)
            job_id_for_update = input_data.get("_job_id") or msg.task_id
            await self._enqueue_write(self.storage.update_task_status, job_id_for_update, "failed", None, err, input_size, wall_ms)
            await self._enqueue_write(
                self.storage.log_execution,
                job_id_for_update, skill_name, caller_node_id, "failed", wall_ms, input_hash, asset_hash, err["message"]
            )
            # v0.33.0: task.failed
            if self.bus:
                self.bus.emit("task.failed", skill_name=skill_name, caller_node=caller_node_id, task_id=job_id_for_update, error_type="MCP_TIMEOUT", identity=caller_node_id)
            is_async = getattr(msg, "mode", "sync") == "async"
            if is_async:
                await self._enqueue_write(self.storage.update_async_job_status, job_id_for_update, "failed", None, err)
                try:
                    await self._sync.enqueue(
                        to_node=caller_node_id,
                        msg_type="knarr/system/task_failed",
                        body={"job_id": job_id_for_update, "skill": skill_name, "error": err},
                        system=True,
                        ttl_hours=24,
                    )
                except Exception as mail_err:
                    logger.warning(f"Async error mail enqueue failed for {job_id_for_update}: {mail_err}")
            # B4: execution_receipt (failed — MCPTimeoutError) in receipt_log
            from datetime import datetime, timezone as _tz_fail2
            _fail2_now = datetime.now(_tz_fail2.utc)
            _fail2_iso = _fail2_now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_fail2_now.microsecond // 1000:03d}Z"
            _fail2_start = _fail2_now - __import__("datetime").timedelta(milliseconds=max(0, min(wall_ms, 86400000)))
            _fail2_start_iso = _fail2_start.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_fail2_start.microsecond // 1000:03d}Z"
            self._write_receipt(
                document_type="execution_receipt",
                payload={
                    "provider": self.node_info.node_id,
                    "caller": caller_node_id,
                    "skill_uri": f"knarr:///{skill_name}",
                    "order_ref": job_id_for_update,
                    "execution": {
                        "status": "failed",
                        "started_at": _fail2_start_iso,
                        "completed_at": _fail2_iso,
                        "duration_ms": wall_ms,
                        "input_hash": f"sha256:{input_hash}" if input_hash else None,
                        "output_hash": None,
                        "error": err.get("message", "mcp_timeout"),
                    },
                    "settlement": {"credit_note_ref": None, "amount": 0.0, "currency": "credits"},
                },
                counterparty=caller_node_id,
                order_ref=job_id_for_update,
                proof_purpose="assertion",
                sign=True,
            )
            result_msg = self._sign(TaskResult(task_id=msg.task_id, status="failed", error=err))
            asyncio.create_task(self._plugins.on_task_complete(
                skill_name, job_id_for_update, caller_node_id, None, wall_ms))

        except asyncio.CancelledError:
            raise

        except Exception as e:
            logger.error(f"Handler error for '{skill_name}': {type(e).__name__}: {e}")
            err = {"code": "HANDLER_ERROR", "message": "Handler execution failed"}
            wall_ms = int((time.time() - start_time) * 1000)
            job_id_for_update = input_data.get("_job_id") or msg.task_id
            await self._enqueue_write(self.storage.update_task_status, job_id_for_update, "failed", None, err, input_size, wall_ms)
            await self._enqueue_write(
                self.storage.log_execution,
                job_id_for_update, skill_name, caller_node_id, "failed", wall_ms, input_hash, asset_hash, str(e)
            )
            # v0.33.0: task.failed
            if self.bus:
                self.bus.emit("task.failed", skill_name=skill_name, caller_node=caller_node_id, task_id=job_id_for_update, error_type="HANDLER_ERROR", identity=caller_node_id)
            is_async = getattr(msg, "mode", "sync") == "async"
            if is_async:
                await self._enqueue_write(self.storage.update_async_job_status, job_id_for_update, "failed", None, err)
                try:
                    await self._sync.enqueue(
                        to_node=caller_node_id,
                        msg_type="knarr/system/task_failed",
                        body={"job_id": job_id_for_update, "skill": skill_name, "error": err},
                        system=True,
                        ttl_hours=24,
                    )
                except Exception as mail_err:
                    logger.warning(f"Async error mail enqueue failed for {job_id_for_update}: {mail_err}")
            # B4: execution_receipt (failed — Exception) in receipt_log
            from datetime import datetime, timezone as _tz_fail3
            _fail3_now = datetime.now(_tz_fail3.utc)
            _fail3_iso = _fail3_now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_fail3_now.microsecond // 1000:03d}Z"
            _fail3_start = _fail3_now - __import__("datetime").timedelta(milliseconds=max(0, min(wall_ms, 86400000)))
            _fail3_start_iso = _fail3_start.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_fail3_start.microsecond // 1000:03d}Z"
            self._write_receipt(
                document_type="execution_receipt",
                payload={
                    "provider": self.node_info.node_id,
                    "caller": caller_node_id,
                    "skill_uri": f"knarr:///{skill_name}",
                    "order_ref": job_id_for_update,
                    "execution": {
                        "status": "failed",
                        "started_at": _fail3_start_iso,
                        "completed_at": _fail3_iso,
                        "duration_ms": wall_ms,
                        "input_hash": f"sha256:{input_hash}" if input_hash else None,
                        "output_hash": None,
                        "error": err.get("message", "handler_error"),
                    },
                    "settlement": {"credit_note_ref": None, "amount": 0.0, "currency": "credits"},
                },
                counterparty=caller_node_id,
                order_ref=job_id_for_update,
                proof_purpose="assertion",
                sign=True,
            )
            result_msg = self._sign(TaskResult(task_id=msg.task_id, status="failed", error=err))
            asyncio.create_task(self._plugins.on_task_complete(
                skill_name, job_id_for_update, caller_node_id, None, wall_ms))

        # Resolve the Future so the fast-path caller gets the response
        if not result_future.done():
            result_future.set_result(result_msg)

        # For slow-path tasks: send result back to requester asynchronously
        if slow:
            try:
                if msg.requester_host and msg.requester_port:
                    if msg.requester_node_id != self.node_info.node_id:
                        await self._send_direct(msg.requester_host, msg.requester_port, result_msg)
            except Exception as e:
                logger.warning(f"Failed to send slow task result to {msg.requester_host}:{msg.requester_port}: {e}")

    async def _execute_local_fast_path(
        self, msg: TaskRequest, handler_fn: Callable, slow: bool,
        skill_name: str, skill_price: float,
        caller_node_id: str, job_id: str, input_hash: str,
    ) -> Message:
        """v0.37.0 A1: Execute local task directly without queue.

        MUST still write receipts, emit bus events, check admission gate.
        This is the fast path for self-calls (caller == self.node_info.node_id).
        """
        from .mcp_bridge import MCPTimeoutError
        import inspect
        from .sidecar import TaskContext

        start_time = time.time()
        input_size = len(json.dumps(msg.input_data)) if msg.input_data else 0

        # v0.33.0 C-track: configurable default timeout
        _default_timeout_s = float(self._config.get("skills", {}).get("default_timeout", 30))
        max_timeout = self._config.get("node", {}).get("max_task_timeout", 3600)
        _req_timeout_s = msg.timeout_ms / 1000.0 if msg.timeout_ms else _default_timeout_s
        handler_timeout = min(_req_timeout_s, max_timeout) if max_timeout > 0 else _req_timeout_s

        # Emit task.started and write order_executing receipt (parity with async path)
        if self.bus:
            self.bus.emit("task.started", skill_name=skill_name, caller_node=caller_node_id, task_id=job_id, fast_path=True, identity=caller_node_id)
        self._write_receipt(
            document_type="order_executing",
            payload={
                "provider": self.node_info.node_id,
                "caller": caller_node_id,
                "skill_uri": f"knarr:///{skill_name}",
                "order_ref": job_id,
                "fast_path": True,
            },
        )

        try:
            loop = asyncio.get_running_loop()

            # Prepare input data (same as _execute_queued_task)
            input_data = dict(msg.input_data)
            # Auto-resolve knarr-asset:// URIs (parity with _execute_queued_task)
            if self._asset_dir:
                for _k, _v in list(input_data.items()):
                    if isinstance(_v, str) and _v.startswith("knarr-asset://"):
                        _hash = _v[len("knarr-asset://"):]
                        if len(_hash) == 64 and all(c in '0123456789abcdef' for c in _hash):
                            _path = os.path.join(self._asset_dir, _hash)
                            if os.path.exists(_path):
                                input_data[_k] = _path
            input_data["_job_id"] = job_id
            input_data["_caller_node_id"] = caller_node_id
            input_data["_node_encrypt"] = self.encrypt_for_peer
            input_data["_node_decrypt"] = self.decrypt_from_peer

            # Check if handler accepts TaskContext
            ctx = TaskContext(self._asset_dir) if self._asset_dir else None
            handler_accepts_ctx = False
            try:
                sig = inspect.signature(handler_fn)
                handler_accepts_ctx = len(sig.parameters) >= 2
            except (ValueError, TypeError):
                pass

            # Execute handler (same logic as _execute_queued_task)
            if asyncio.iscoroutinefunction(handler_fn):
                def _run_async():
                    tloop = asyncio.new_event_loop()
                    try:
                        if handler_accepts_ctx:
                            return tloop.run_until_complete(handler_fn(input_data, ctx))
                        else:
                            return tloop.run_until_complete(handler_fn(input_data))
                    finally:
                        tloop.close()
                result_data = await asyncio.wait_for(
                    loop.run_in_executor(self._handler_pool, _run_async),
                    timeout=handler_timeout
                )
            else:
                if handler_accepts_ctx:
                    result_data = await asyncio.wait_for(
                        loop.run_in_executor(self._handler_pool, handler_fn, input_data, ctx),
                        timeout=handler_timeout
                    )
                else:
                    result_data = await asyncio.wait_for(
                        loop.run_in_executor(self._handler_pool, handler_fn, input_data),
                        timeout=handler_timeout
                    )

            wall_ms = int((time.time() - start_time) * 1000)
            logger.info(f"FAST_PATH completed: skill={skill_name} task={job_id[:8]} wall={wall_ms}ms")

            # Strip non-serializable hooks from result
            if isinstance(result_data, dict):
                result_data = {k: v for k, v in result_data.items() if not callable(v)}

            # Egress check
            if result_data and hasattr(self, '_egress'):
                out_str = json.dumps(result_data) if isinstance(result_data, dict) else str(result_data)
                if not self._egress.check(out_str):
                    logger.critical(f"EGRESS_BLOCK_RESULT task={job_id[:16]} skill={skill_name}")
                    if self.bus:
                        self.bus.emit("security.egress_blocked", skill_name=skill_name, target=caller_node_id, identity=caller_node_id)
                    result_data = {"error": "SECURITY_VIOLATION", "code": "EGRESS_FILTER_BLOCKED"}

            # Write task status and execution log
            await self._enqueue_write(
                self.storage.update_task_status, job_id, "completed",
                result_data, None, input_size, wall_ms
            )
            await self._enqueue_write(
                self.storage.log_execution,
                job_id, skill_name, caller_node_id, "completed", wall_ms, input_hash, None, None, skill_price, ""
            )

            # Emit bus event
            if self.bus:
                self.bus.emit("task.completed", skill_name=skill_name, caller_node=caller_node_id, task_id=job_id, wall_ms=wall_ms, price=skill_price, identity=caller_node_id)

            # Write execution receipt (same as async path)
            output_hash = hashlib.sha256(
                json.dumps(result_data, sort_keys=True, separators=(',', ':')).encode()
            ).hexdigest() if result_data else ""

            from datetime import datetime, timezone as _tz_exec
            _completed_at = datetime.now(_tz_exec.utc)
            _completed_iso = _completed_at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_completed_at.microsecond // 1000:03d}Z"
            _started_at = _completed_at - __import__("datetime").timedelta(milliseconds=max(0, min(wall_ms, 86400000)))
            _started_iso = _started_at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_started_at.microsecond // 1000:03d}Z"

            self._write_receipt(
                document_type="execution_receipt",
                payload={
                    "provider": self.node_info.node_id,
                    "caller": caller_node_id,
                    "skill_uri": f"knarr:///{skill_name}",
                    "order_ref": job_id,
                    "execution": {
                        "status": "completed",
                        "started_at": _started_iso,
                        "completed_at": _completed_iso,
                        "duration_ms": wall_ms,
                        "input_hash": f"sha256:{input_hash}" if input_hash else None,
                        "output_hash": f"sha256:{output_hash}" if output_hash else None,
                        "error": None,
                    },
                    "settlement": {
                        "credit_note_ref": None,
                        "amount": float(skill_price),
                        "currency": "credits",
                    },
                },
                counterparty=caller_node_id,
                order_ref=job_id,
                proof_purpose="assertion",
                sign=True,
            )

            # Return success result
            return self._sign(TaskResult(task_id=job_id, status="completed", output_data=result_data))

        except asyncio.TimeoutError:
            err = {"code": "TIMEOUT", "message": f"Handler exceeded {handler_timeout}s timeout"}
            wall_ms = int((time.time() - start_time) * 1000)
            await self._enqueue_write(
                self.storage.update_task_status, job_id, "failed", None, err, input_size, wall_ms
            )
            if self.bus:
                self.bus.emit("task.failed", skill_name=skill_name, caller_node=caller_node_id, task_id=job_id, error_type="TIMEOUT", identity=caller_node_id)
            return self._sign(TaskResult(task_id=job_id, status="failed", error=err))

        except Exception as e:
            logger.exception(f"FAST_PATH_ERROR: skill={skill_name} task={job_id[:8]}: {e}")
            err = {"code": "HANDLER_ERROR", "message": str(e)}
            wall_ms = int((time.time() - start_time) * 1000)
            await self._enqueue_write(
                self.storage.update_task_status, job_id, "failed", None, err, input_size, wall_ms
            )
            if self.bus:
                self.bus.emit("task.failed", skill_name=skill_name, caller_node=caller_node_id, task_id=job_id, error_type="EXCEPTION", identity=caller_node_id)
            return self._sign(TaskResult(task_id=job_id, status="failed", error=err))

    async def _send_direct(self, host: str, port: int, msg: Message):
        """Sends a message directly to a host:port without expecting a Knarr response."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5.0
            )
            try:
                await send_message(writer, msg)
            finally:
                writer.close()
                await writer.wait_closed()
        except Exception as e:
            logger.debug(f"Direct send to {host}:{port} failed: {e}")

    def _load_or_generate_node_id(self) -> str:
        """Loads node identity and derives Ed25519 keypair."""
        key_bytes = self.storage.get_node_key()
        if not key_bytes:
            key_bytes = secrets.token_bytes(32)
            self.storage.set_node_key(key_bytes)
            logger.info("Generated new persistent node identity")
        else:
            logger.info("Loaded persistent node identity")

        self._signing_key = SigningKey(key_bytes)
        
        # Register node seed as sensitive material for egress filter
        self._egress.register_sensitive_material(key_bytes)
        
        from ..core.vault import KeyringVault
        data_dir = self._config.get("_data_dir", self._config.get("_config_dir", "."))
        os.makedirs(data_dir, exist_ok=True)
        vault_path = os.path.join(data_dir, "vault.db")
        self._vault = KeyringVault(vault_path, key_bytes)
        # C2: wire vault into SecretsManager
        self._secrets_mgr.set_vault(self._vault)

        verify_key = self._signing_key.verify_key
        self._public_key_hex = verify_key.encode().hex()

        # Derive Solana wallet address from Ed25519 public key
        from ..core.wallet import derive_solana_address
        self._solana_address = derive_solana_address(self._signing_key)

        # Wallet: always derived from identity key (peers verify derivation)
        manual_wallet = self._config.get("node", {}).get("wallet", "")
        if manual_wallet:
            if manual_wallet != self._solana_address:
                logger.warning(f"Config [node] wallet ignored — peers reject wallets that don't match the identity key. Using derived address.")
            self._wallet = self._solana_address
        else:
            self._wallet = self._solana_address
            logger.info(f"Solana wallet: {self._wallet} (auto-derived)")

        # Token config (read-only in 12a)
        token_cfg = self._config.get("token", {})
        from ..core.constants import KNARR_MINT
        # Deprecated: [token] mint config key. Ignored — KNARR_MINT is a protocol constant.
        old_mint = token_cfg.get("mint", "")
        if old_mint:
            logger.warning("Config [token] mint is deprecated and ignored — KNARR_MINT is a protocol constant")
        self._token_mint = KNARR_MINT
        self._rpc_url = token_cfg.get("rpc_url", "") or None  # None = use default
        self._token_balance: Optional[float] = None
        self._sol_balance: Optional[float] = None
        self._balance_last_refresh: float = 0.0

        return hashlib.sha256(verify_key.encode()).hexdigest()

    def _cleanup_zombie_tasks(self):
        """Fix #13: Transition zombie 'running' tasks in execution_log and async_jobs to 'failed'."""
        conn = self.storage._get_conn()
        now = time.time()
        configured_ms = self._config.get("task", {}).get("max_task_timeout", 300000)
        timeout_sec = max(configured_ms // 1000, 300) if configured_ms else 86400
        cursor = conn.execute(
            "UPDATE execution_log SET status = 'failed', error = ? WHERE status = 'running' AND (created_at + ?) < ?",
            (json.dumps({"error": "node_restart"}), timeout_sec, now)
        )
        conn.commit()
        if cursor.rowcount > 0:
            logger.warning(f"Cleaned up {cursor.rowcount} zombie tasks in execution_log")

        cursor2 = conn.execute(
            "UPDATE async_jobs SET status = 'failed' "
            "WHERE status IN ('running', 'accepted') AND (created_at + ?) < ?",
            (timeout_sec, now)
        )
        conn.commit()
        if cursor2.rowcount > 0:
            logger.warning(f"Cleaned up {cursor2.rowcount} zombie async jobs")

    def _init_encryption(self):
        """Derives X25519 keys from Ed25519 signing key for node-level encryption."""
        if not self._signing_key:
            return
        self._x25519_private = self._signing_key.to_curve25519_private_key()
        self._x25519_public = self._signing_key.verify_key.to_curve25519_public_key()
        self._encryption_key_hex = self._x25519_public.encode().hex()
        # Register X25519 private key bytes with egress filter
        if hasattr(self, '_egress'):
            self._egress.register_sensitive_material(self._x25519_private.encode())
        if getattr(self, "_enc_debug", False):
            logger.info(f"[ENC_INIT] Derived X25519 public key: {self._encryption_key_hex[:16]}...")

    def encrypt_for_peer(self, data: bytes, node_id: str) -> bytes:
        """Encrypts data for a peer using X25519 SealedBox."""
        if not self._signing_key:
            raise RuntimeError("Encryption not initialized")
        peer_key = self.storage.get_peer_encryption_key(node_id)
        if not peer_key:
            raise ValueError(f"No encryption key found for peer {node_id}")
        from nacl.public import SealedBox, PublicKey
        box = SealedBox(PublicKey(bytes.fromhex(peer_key)))
        return box.encrypt(data)

    def decrypt_from_peer(self, data: bytes) -> bytes:
        """Decrypts SealedBox data sent to this node."""
        if not self._x25519_private:
            raise RuntimeError("Encryption not initialized")
        from nacl.public import SealedBox
        box = SealedBox(self._x25519_private)
        return box.decrypt(data)

    def _sign_receipt(self, task_id: str, skill_name: str,
                      consumer_node_id: str, credits_charged: float,
                      input_hash: str, output_hash: str, wall_ms: int,
                      price_breakdown_json: str = None) -> str:
        """Generates a signed execution receipt for a completed task."""
        if not self._signing_key:
            return ""
        import base64
        payload_dict = {
            "task_id": task_id,
            "skill_name": skill_name,
            "provider_node_id": self.node_info.node_id,
            "consumer_node_id": consumer_node_id,
            "credits_charged": credits_charged,
            "input_hash": input_hash,
            "output_hash": output_hash,
            "wall_ms": wall_ms,
            "price_breakdown": json.loads(price_breakdown_json) if price_breakdown_json else None,
            "timestamp": int(time.time()),
        }
        payload_bytes = json.dumps(payload_dict, sort_keys=True, separators=(',', ':')).encode('utf-8')
        signature = self._signing_key.sign(payload_bytes).signature

        receipt_dict = {"data": payload_dict, "signature": base64.b64encode(signature).decode('utf-8')}
        return json.dumps(receipt_dict, sort_keys=True, separators=(',', ':'))

    def _write_receipt(
        self,
        document_type: str,
        payload: dict,
        counterparty: Optional[str] = None,
        order_ref: Optional[str] = None,
        proof_purpose: str = "assertion",
        sign: bool = False,
    ) -> str:
        """Write a receipt to the append-only receipt_log.

        Single entry point for all receipt writes. Generates receipt_id,
        stamps timestamp, enriches payload with W3C Data Integrity fields,
        signs if requested, delegates to storage.write_receipt().

        Args:
            document_type:  e.g. "execution_receipt", "mail_delivery_receipt"
            payload:        Domain fields dict. Mutated in-place with common fields.
            counterparty:   Other party node_id hex, or None for local records.
            order_ref:      Task/job ID this receipt tracks, or None.
            proof_purpose:  "assertion" or "acknowledgment".
            sign:           True to sign with this node's Ed25519 key.

        Returns:
            The generated receipt_id string.
        """
        import secrets as _secrets
        from datetime import datetime, timezone as _tz

        _prefix_map = {
            "execution_receipt": "exec",
            "credit_note": "cn",
            "mail_delivery_receipt": "mdr",
            "mail_receive_receipt": "mrr",
            "order_ack": "oack",
            "order_executing": "oexe",
        }
        type_prefix = _prefix_map.get(document_type, "rct")
        receipt_id = f"{type_prefix}_{_secrets.token_hex(8)}"  # L-12: 64-bit entropy (collision at ~4B)

        _now = datetime.now(_tz.utc)
        timestamp = _now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_now.microsecond // 1000:03d}Z"

        payload = dict(payload)  # FIX-03: don't mutate caller's dict
        payload["document_type"] = document_type
        payload["version"] = 1
        payload["receipt_id"] = receipt_id
        payload["timestamp"] = timestamp
        if sign and self._signing_key:
            payload["cryptosuite"] = "ed25519-jcs"
        payload["proof_purpose"] = proof_purpose

        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)

        signature: Optional[str] = None
        if sign and self._signing_key:
            raw_sig = self._signing_key.sign(payload_json.encode("utf-8")).signature
            signature = "ed25519:" + raw_sig.hex()

        logger.debug(
            f"RECEIPT_WRITE type={document_type} id={receipt_id} "
            f"order={str(order_ref)[:8] if order_ref else 'none'} signed={sign}"
        )

        try:
            self.storage.write_receipt(
                receipt_id=receipt_id,
                document_type=document_type,
                timestamp=timestamp,
                identity=self.node_info.node_id,
                counterparty=counterparty,
                order_ref=order_ref,
                proof_purpose=proof_purpose,
                payload_json=payload_json,
                signature=signature,
            )
        except Exception as _exc:
            logger.warning(f"RECEIPT_WRITE_FAIL type={document_type} id={receipt_id}: {_exc}")
            if self.bus:
                self.bus.emit("receipt.write_failed", document_type=document_type, receipt_id=receipt_id, error=str(_exc), identity=self.node_info.node_id)

        return receipt_id

    async def _wm_ingest(self, document: dict, originator_pubkey: bytes):
        if getattr(self, "wm", None) is None:
            return None

        from ..core.proof import sign_document
        from ..core.warehouse_manager import IngestResult

        doc_type = document.get("document_type", document.get("type", "unknown"))
        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(self._handler_pool, self.wm.ingest, document, originator_pubkey),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"WM_INGEST_TIMEOUT type={doc_type}")
            return IngestResult(
                status="rejected",
                document_type=doc_type,
                quarantine_id=None,
                gate_results={},
                reason="timeout",
            )
        except Exception as exc:
            logger.error(f"WM_INGEST_FAIL type={doc_type}: {exc}", exc_info=True)
            return IngestResult(
                status="rejected",
                document_type=doc_type,
                quarantine_id=None,
                gate_results={},
                reason=str(exc),
            )

        if result.status == "promoted" and result.needs_countersign:
            try:
                if self._signing_key is None:
                    raise RuntimeError("missing node signing key for countersign")
                payload = {
                    key: value
                    for key, value in (result.document or document).items()
                    if key != "proof"
                }
                signed_doc = sign_document(
                    payload,
                    self._signing_key,
                    f"did:knarr:{self.node_info.node_id}#key-1",
                )
                result = replace(result, document=signed_doc)
            except Exception as exc:
                logger.error(f"WM_COUNTERSIGN_FAIL type={doc_type}: {exc}", exc_info=True)
                return IngestResult(
                    status="rejected",
                    document_type=result.document_type,
                    quarantine_id=result.quarantine_id,
                    gate_results=result.gate_results,
                    reason=str(exc),
                )

        return result

    def _init_group_engine(self):
        """Initialize GroupEngine from config. Plugin can override later."""
        from ..core.groups import DefaultGroupEngine
        config_groups: Dict[str, Set[str]] = {}

        # Read new format: [groups.X]
        for name, cfg in self._config.get("groups", {}).items():
            if not isinstance(cfg, dict):
                continue
            members = set(cfg.get("members", []))
            mf = cfg.get("members_file")
            if mf:
                config_dir = self._config.get("_config_dir", os.getcwd())
                mf_path = os.path.join(config_dir, mf) if not os.path.isabs(mf) else mf
                mf_path = os.path.abspath(mf_path)
                config_dir_abs = os.path.abspath(config_dir)
                if mf_path.startswith(config_dir_abs + os.sep) or mf_path == config_dir_abs:
                    if os.path.exists(mf_path):
                        with open(mf_path) as f:
                            for line in f:
                                line = line.strip()
                                if line and not line.startswith("#"):
                                    members.add(line)
                    else:
                        logger.warning(f"Group '{name}' members_file not found: {mf}")
                else:
                    logger.warning(f"Group '{name}' members_file escapes config directory: {mf}")
            config_groups[name] = members

        # Read old format: [policy.group.X] — backward compat
        for name, cfg in self._config.get("policy", {}).get("group", {}).items():
            if name not in config_groups:  # new format takes precedence
                if not isinstance(cfg, dict):
                    continue
                members = set(cfg.get("members", []))
                mf = cfg.get("members_file")
                if mf:
                    config_dir = self._config.get("_config_dir", os.getcwd())
                    mf_path = os.path.join(config_dir, mf) if not os.path.isabs(mf) else mf
                    mf_path = os.path.abspath(mf_path)
                    config_dir_abs = os.path.abspath(config_dir)
                    if mf_path.startswith(config_dir_abs + os.sep) or mf_path == config_dir_abs:
                        if os.path.exists(mf_path):
                            with open(mf_path) as f:
                                for line in f:
                                    line = line.strip()
                                    if line and not line.startswith("#"):
                                        members.add(line)
                config_groups[name] = members

        self._group_engine = DefaultGroupEngine(config_groups)
        logger.info(f"GroupEngine initialized: {len(config_groups)} groups from config")

    async def submit_async_task(
        self, provider_node_id: str, provider_host: str, provider_port: int,
        skill_name: str, input_data: Dict[str, Any], timeout_ms: int = 30000
    ) -> TaskStatus:
        """Submits a task for asynchronous execution (non-blocking)."""
        task_id = str(uuid.uuid4())
        req = self._sign(TaskRequest(
            task_id=task_id,
            requester_node_id=self.node_info.node_id,
            requester_host=self.node_info.host,
            requester_port=self.node_info.port,
            skill_name=skill_name,
            input_data=input_data,
            timeout_ms=timeout_ms,
            mode="async"
        ))
        
        resp = await request_response(provider_host, provider_port, req, timeout=10.0)
        if isinstance(resp, TaskStatus) and verify_message(resp):
            return resp
        elif isinstance(resp, TaskResult) and verify_message(resp):
            # If provider finished it instantly (e.g. dedup)
            return TaskStatus(task_id=resp.task_id, status=resp.status)
        else:
            raise RuntimeError(f"Unexpected response type: {type(resp).__name__}")

    def _sign(self, msg: Message) -> Message:
        """Signs an outbound message with this node's key."""
        return sign_message(msg, self._signing_key)

    def _emit_task_rejected(self, skill: str, caller: str, task_id: str, reason: str):
        """v0.33.0: Helper to emit task.rejected from all 6 rejection paths."""
        if self.bus:
            self.bus.emit("task.rejected", skill_name=skill, caller_node=caller, task_id=task_id, reason=reason, identity=caller)

    def _check_credit_restored(self, peer_public_key: str, old_balance: float, new_balance: float):
        """v0.33.0: Emit credit.restored when peer moves from over-threshold to under-threshold."""
        if not self.bus:
            return
        initial_credit, min_balance = self._resolve_policy(peer_public_key, "")
        credit_range = initial_credit - min_balance
        if credit_range <= 0:
            return
        threshold = self._get_settlement_config().get("tab_reminder_threshold", 80.0)
        old_util = max(0.0, min(100.0, ((initial_credit - old_balance) / credit_range) * 100.0))
        new_util = max(0.0, min(100.0, ((initial_credit - new_balance) / credit_range) * 100.0))
        if old_util >= threshold and new_util < threshold:
            self.bus.emit("credit.restored", counterparty=peer_public_key, new_utilization=new_util, identity=peer_public_key)

    async def start(self):
        """Starts the server and background tasks."""
        self.server = await asyncio.start_server(
            self._handle_connection, self._bind_host, self.node_info.port
        )
        # Update port if dynamic (0)
        sock = self.server.sockets[0]
        self.node_info = NodeInfo(
            node_id=self.node_info.node_id,
            host=self.node_info.host,
            port=sock.getsockname()[1]
        )
        
        addr = self.server.sockets[0].getsockname()
        logger.info(f"DHT node {self.node_info.node_id} serving on {addr}")
        
        # Auto-generate TLS cert if missing (only for default paths, not custom)
        config_dir = self._config.get("_config_dir", os.getcwd())
        data_dir = self._config.get("_data_dir", config_dir)
        from ..mail.tls import generate_tls_cert, resolve_cert_paths
        cert_path, key_path = resolve_cert_paths(self._config, data_dir)
        network_cfg = self._config.get("network", {})
        custom_tls = "tls_cert" in network_cfg or "tls_key" in network_cfg
        if not custom_tls and (not os.path.exists(cert_path) or not os.path.exists(key_path)):
            key_bytes = self.storage.get_node_key()
            if key_bytes:
                generate_tls_cert(key_bytes, self.node_info.node_id, data_dir)
                self._generated_identity_certs = True
        self._cert_path = cert_path
        self._key_path = key_path

        # Start sidecar (default: protocol port + 1, set to 0 to disable)
        sidecar_port = self._config.get("node", {}).get("sidecar_port")
        if sidecar_port is None:
            sidecar_port = self.node_info.port + 1  # default: port + 1
        else:
            sidecar_port = int(sidecar_port)
        if sidecar_port > 0:
            asset_dir = self._config.get("sidecar", {}).get("asset_dir", "assets")
            # Resolve relative to config directory (same as handler paths)
            if not os.path.isabs(asset_dir):
                asset_dir = os.path.join(config_dir, asset_dir)
            self._asset_dir = asset_dir
            self._sidecar = AssetSidecar(
                host=self._bind_host,
                port=sidecar_port,
                asset_dir=asset_dir,
                signing_key=self._signing_key,
                max_asset_size=self._config.get("node", {}).get("max_asset_size", 104857600),
                max_total_size=self._config.get("sidecar", {}).get("max_total_size", 1073741824),
                asset_ttl=self._config.get("sidecar", {}).get("asset_ttl", 3600),
                cert_path=cert_path,
                key_path=key_path,
            )
            await self._sidecar.start()
            self._sidecar_port = self._sidecar.port
        self._mail_handlers.bind_runtime(
            asset_dir=self._asset_dir,
            sidecar=self._sidecar,
            signing_key=self._signing_key,
        )

        self._running = True
        self._start_time = time.monotonic()

        # v0.23.0: Encryption and zombie task cleanup
        self._cleanup_zombie_tasks()
        self._init_encryption()

        # v0.22.0: Initialize group engine before plugins load
        self._init_group_engine()

        # V015: Load plugins
        self._plugins.load_plugins()

        # v0.22.0: Pass group engine and storage path to plugin contexts
        storage_path_str = self.storage.db_path if hasattr(self.storage, 'db_path') else None  # F-2 fix
        # v0.35.0: Create commerce bridge callbacks
        _sign_cb = None
        _query_cb = None
        if self._signing_key:
            from knarr.commerce.plugin_bridge import make_sign_callback, make_query_receipts_callback
            _sign_cb = make_sign_callback(self._signing_key, self.node_info.node_id)
            _query_cb = make_query_receipts_callback(self.storage)
        for plugin in self._plugins.plugins:
            ctx = plugin._ctx if hasattr(plugin, '_ctx') else None
            if ctx is not None:
                if ctx.storage_path is None:
                    ctx.storage_path = storage_path_str
                if hasattr(self, '_sync'):
                    ctx.register_mail_handler = self._sync.register_handler
                    ctx.send_mail = self._sync.enqueue
                ctx.sign_document = _sign_cb       # v0.35.0
                ctx.query_receipts = _query_cb     # v0.35.0
                ctx.economy_config = dict(self._config.get("economy", {}))  # v0.42.0
                # If plugin set itself as group_engine, pick it up
                if ctx.group_engine is not None:
                    self._group_engine = ctx.group_engine
                    logger.info(f"GroupEngine upgraded by plugin: {plugin.__class__.__name__}")

        # F-1 fix: propagate final group_engine to ALL plugin contexts
        if self._group_engine is not None:
            for plugin in self._plugins.plugins:
                ctx = plugin._ctx if hasattr(plugin, '_ctx') else None
                if ctx is not None and ctx.group_engine is None:
                    ctx.group_engine = self._group_engine

        # v0.22.0: Pass node group config to groups plugin
        # Normalize members_file paths to absolute (config_dir base) so plugin
        # doesn't need to know the config directory (F-4: path resolution divergence)
        config_dir = self._config.get("_config_dir", os.getcwd())
        for plugin in self._plugins.plugins:
            if hasattr(plugin, '_load_explicit_groups') and hasattr(plugin, '_group_defs'):
                groups_cfg = {}
                for gname, gcfg in self._config.get("groups", {}).items():
                    if isinstance(gcfg, dict) and gcfg.get("members_file"):
                        gcfg = dict(gcfg)  # shallow copy to avoid mutating original
                        mf = gcfg["members_file"]
                        if not os.path.isabs(mf):
                            gcfg["members_file"] = os.path.abspath(os.path.join(config_dir, mf))
                    groups_cfg[gname] = gcfg
                plugin._config["groups"] = groups_cfg
                # Also merge old-format groups
                for name, cfg in self._config.get("policy", {}).get("group", {}).items():
                    if name not in plugin._config.get("groups", {}):
                        mf = cfg.get("members_file")
                        if mf and not os.path.isabs(mf):
                            mf = os.path.abspath(os.path.join(config_dir, mf))
                        plugin._config.setdefault("groups", {})[name] = {
                            "type": "explicit",
                            "members": list(cfg.get("members", [])),
                            "members_file": mf,
                        }
                plugin._group_defs.clear()
                plugin._cache.clear()
                plugin._load_explicit_groups()
                # Load computed group defs (don't evaluate yet — on_tick will)
                for gname, gcfg in plugin._config.get("groups", {}).items():
                    if isinstance(gcfg, dict) and gcfg.get("type") == "computed":
                        plugin._group_defs[gname] = gcfg

        own_skills = self.storage.get_own_skills()
        for skill in own_skills:
            self._own_skills[skill.name] = skill
        logger.info(f"Reloaded {len(self._own_skills)} own skills from storage")
        
        self.background_tasks.append(asyncio.create_task(self._heartbeat_loop()))
        self.background_tasks.append(asyncio.create_task(self._settlement_consumer_loop()))
        self.background_tasks.append(asyncio.create_task(self._republish_loop()))
        self.background_tasks.append(asyncio.create_task(self._prune_loop()))
        self.background_tasks.append(asyncio.create_task(self._writer_loop()))
        self.background_tasks.append(asyncio.create_task(self._event_loop_watchdog()))
        self.background_tasks.append(asyncio.create_task(self._stale_task_watchdog()))

        for i in range(self._task_slots):
            self.background_tasks.append(asyncio.create_task(self._task_worker_loop(i)))

        self.background_tasks.append(asyncio.create_task(self._mail_ttl_cleanup()))
        self.background_tasks.append(asyncio.create_task(self._auto_upgrade_loop()))
        if self._wallet and self._token_mint:
            self.background_tasks.append(asyncio.create_task(self._balance_refresh_loop()))

        # v0.41.0 A2: Independent background loops for network I/O (extracted from _heartbeat_tick)
        self.background_tasks.append(asyncio.create_task(self._flush_outbox_loop()))
        self.background_tasks.append(asyncio.create_task(self._pull_from_correspondents_loop()))
        self.background_tasks.append(asyncio.create_task(self._peer_heartbeat_sweep_loop()))

        if self.wm is not None:
            try:
                pending = self.storage.quarantine_list_pending()
                for row in pending:
                    self.bus.emit(
                        f"wm.held.{row['document_type']}",
                        document_type=row["document_type"],
                        quarantine_id=row["id"],
                        identity=self.node_info.node_id,
                    )
                if pending:
                    logger.info(f"WM recovery re-emitted {len(pending)} held document(s)")
            except Exception as exc:
                logger.warning(f"WM_RECOVERY_ERROR: {exc}")

    async def stop(self):
        """Stops the server, MCP bridges, and background tasks."""
        self._running = False

        # V015: Plugin shutdown
        await self._plugins.on_shutdown()

        if self._sidecar:
            await self._sidecar.stop()
        
        for bridge in self._mcp_bridges:
            await bridge.stop()
        self._mcp_bridges.clear()

        for task in self.background_tasks:
            task.cancel()
        await asyncio.gather(*self.background_tasks, return_exceptions=True)

        await self._pool.close_all()

        if self.server:
            self.server.close()
            await self.server.wait_closed()
        self._handler_pool.shutdown(wait=False)
        if self._vault:
            self._vault.close()
        self.storage.close()
        logger.info(f"DHT node {self.node_info.node_id} stopped")

    async def reconnect_from_cache(self, max_age_hours: float = 24) -> bool:
        """Try cached peers from previous sessions before bootstrap.

        Returns True if any cached peer responded with a valid JoinResponse.
        """
        # Purge very old entries first (>72h)
        purged = self.storage.purge_stale_peers(max_age_hours=72)
        if purged:
            logger.info(f"PEER_CACHE purged={purged} stale entries")

        cached = self.storage.get_cached_peers(max_age_hours=max_age_hours, limit=10)
        if not cached:
            return False

        logger.info(f"PEER_CACHE trying {len(cached)} cached peers")
        for peer in cached:
            if peer.node_id == self.node_info.node_id:
                continue
            try:
                host, port = self.resolve_peer(peer.node_id, peer.host, peer.port)
                req = self._sign(JoinRequest(
                    node_id=self.node_info.node_id,
                    host=self.node_info.host,
                    port=self.node_info.port,
                    ephemeral=self._ephemeral,
                ))
                resp = await request_response(host, port, req, timeout=3.0)
                if isinstance(resp, JoinResponse) and verify_message(resp):
                    for peer_dict in resp.peers:
                        try:
                            p = NodeInfo(**peer_dict)
                            if p.node_id == self.node_info.node_id:
                                continue
                            if not self._validate_peer_fields(p.node_id, p.host, p.port):
                                continue
                            await self._enqueue_write_proto(self.storage.upsert_peer, p)
                        except (TypeError, KeyError):
                            continue
                    logger.info(f"PEER_CACHE reconnected via {host}:{port} ({peer.node_id[:16]}), discovered {len(resp.peers)} peers")
                    # Startup sync from this peer
                    try:
                        sync_req = self._sign(SyncRequest(since=0.0))
                        sync_resp = await request_response(host, port, sync_req, timeout=10.0)
                        if isinstance(sync_resp, SyncResponse) and verify_message(sync_resp):
                            await self._process_sync_response(sync_resp)
                    except Exception as e:
                        logger.warning(f"PEER_CACHE startup sync failed: {e}")
                    return True
            except Exception:
                continue
        logger.info("PEER_CACHE no cached peer responded, falling back to bootstrap")
        return False

    async def join(self, bootstrap_peers: List[str]):
        """Joins the network and re-announces own skills."""
        self._bootstrap_peers = list(bootstrap_peers)
        joined = False
        for peer_addr in bootstrap_peers:
            try:
                host, port_str = peer_addr.split(":")
                port = int(port_str)
                
                req = self._sign(JoinRequest(
                    node_id=self.node_info.node_id,
                    host=self.node_info.host,
                    port=self.node_info.port,
                    ephemeral=self._ephemeral
                ))
                
                resp = await request_response(host, port, req)
                if isinstance(resp, JoinResponse) and verify_message(resp):
                    for peer_dict in resp.peers:
                        try:
                            peer = NodeInfo(**peer_dict)
                            if peer.node_id == self.node_info.node_id:
                                continue
                            if not self._validate_peer_fields(peer.node_id, peer.host, peer.port):
                                continue
                            await self._enqueue_write_proto(self.storage.upsert_peer, peer)
                        except (TypeError, KeyError):
                            continue
                    logger.info(f"Joined network via {peer_addr}, discovered {len(resp.peers)} peers")
                    joined = True
                    
                    # Startup sync
                    try:
                        sync_req = self._sign(SyncRequest(since=0.0))
                        sync_resp = await request_response(host, port, sync_req, timeout=10.0)
                        if isinstance(sync_resp, SyncResponse) and verify_message(sync_resp):
                            await self._process_sync_response(sync_resp)
                    except Exception as e:
                        logger.warning(f"Startup sync failed: {e}")
                    
                    break
            except Exception as e:
                logger.warning(f"Failed to join via {peer_addr}: {e}")
        
        if joined:
            await self._reannounce_all()
            
        return joined

    async def _process_sync_response(self, resp: SyncResponse):
        """Processes a sync response, verifying each entry's signature."""
        stored = 0
        dropped = 0
        for entry in resp.skills:
            announce = Announce(
                msg_id=entry.get("msg_id", str(uuid.uuid4())),
                node_id=entry["provider_node_id"],
                skill_key=entry["skill_key"],
                skill_sheet=entry["skill_sheet"],
                hops=0,
                public_key=entry.get("public_key", ""),
                signature=entry.get("signature", ""),
                encryption_key=entry.get("encryption_key", ""),
                wallet=entry.get("wallet", ""),
                provider_host=entry.get("provider_host", ""),
                provider_port=entry.get("provider_port", 0),
            )

            if not verify_message(announce) or not verify_node_id(announce):
                dropped += 1
                continue

            try:
                skill_sheet = validate_skill_sheet(entry["skill_sheet"])
                sync_ttl = entry.get("ttl", self._get_skill_ttl())
                await self._enqueue_write(
                    self.storage.upsert_skill,
                    entry["skill_key"],
                    entry["provider_node_id"],
                    skill_sheet,
                    sync_ttl,
                    False, # is_own
                    entry.get("public_key"),
                    entry.get("signature"),
                    entry.get("msg_id"),
                    entry.get("sidecar_port", 0),
                    entry.get("provider_host", ""),
                    entry.get("provider_port", 0),
                )
                logger.debug(f"SKILL_SYNC_STORE key={entry['skill_key']} from={entry['provider_node_id'][:16]} ttl={sync_ttl}")
                stored += 1
            except Exception:
                dropped += 1

        logger.info(f"Startup sync: stored {stored} skills, dropped {dropped} invalid entries")

    @property
    def _node_jurisdiction_wire(self) -> str:
        """Node jurisdiction as comma-separated string for wire format."""
        raw = self._config.get("node", {}).get("jurisdiction", "")
        if isinstance(raw, list):
            return ",".join(raw)
        return str(raw)

    async def _reannounce_all(self):
        """Re-announces all own skills to all known peers."""
        if self._version_gated:
            return
        peers = self.storage.get_peers()
        if not peers:
            return

        announced = 0
        for skill_name, skill_sheet in self._own_skills.items():
            visibility = self._skill_visibility.get(skill_name.lower(), "public")
            if visibility == "private":
                continue

            msg = self._sign(Announce(
                node_id=self.node_info.node_id,
                skill_key=skill_name,
                skill_sheet=skill_sheet.to_dict(),
                sidecar_port=self._sidecar_port,
                encryption_key=self._encryption_key_hex,
                wallet=self._wallet,
                provider_host=self.node_info.host,
                provider_port=self.node_info.port,
                jurisdiction=self._node_jurisdiction_wire,
            ))
            targets = random.sample(peers, min(self._gossip_fanout, len(peers)))
            for peer in targets:
                asyncio.create_task(self._send_to_peer(peer, msg))
            announced += 1
        logger.info(f"REPUBLISH_CYCLE skills={announced} peers={len(peers)} fanout={self._gossip_fanout}")

    async def announce(self, skill_sheet_data: Dict[str, Any]):
        """Validates, stores, and announces a skill."""
        try:
            # E-3: Inherit node-level jurisdiction if skill doesn't specify its own
            if not skill_sheet_data.get("jurisdiction"):
                node_jurisdiction = self._node_jurisdiction_wire
                if node_jurisdiction:
                    skill_sheet_data = dict(skill_sheet_data)
                    skill_sheet_data["jurisdiction"] = node_jurisdiction
            skill_sheet = validate_skill_sheet(skill_sheet_data)
            self._own_skills[skill_sheet.name] = skill_sheet
            
            visibility = self._skill_visibility.get(skill_sheet.name.lower(), "public")
            if visibility == "private":
                # Store locally but don't announce to DHT
                await self._enqueue_write(
                    self.storage.upsert_skill,
                    skill_sheet.name, self.node_info.node_id, skill_sheet,
                    self._get_skill_ttl(), # ttl
                    True, # is_own
                    self._public_key_hex,
                    "", # no signature for private skills
                    str(uuid.uuid4()),
                    self._sidecar_port
                )
                logger.info(f"Registered private skill '{skill_sheet.name}' locally")
                self.refresh_node_meta()  # v0.29.0: skills_count changed
                return skill_sheet.name

            msg = self._sign(Announce(
                node_id=self.node_info.node_id,
                skill_key=skill_sheet.name,
                skill_sheet=skill_sheet.to_dict(),
                sidecar_port=self._sidecar_port,
                encryption_key=self._encryption_key_hex,
                wallet=self._wallet,
                provider_host=self.node_info.host,
                provider_port=self.node_info.port,
                jurisdiction=self._node_jurisdiction_wire,
            ))

            # Store locally with signature info
            await self._enqueue_write(
                self.storage.upsert_skill,
                skill_sheet.name, self.node_info.node_id, skill_sheet,
                self._get_skill_ttl(), # ttl
                True, # is_own
                msg.public_key,
                msg.signature,
                msg.msg_id,
                msg.sidecar_port,
                self.node_info.host,
                self.node_info.port,
            )
            
            # Update meta cache so /meta/skill/{name} serves fresh data
            self._update_meta_cache("skill", skill_sheet.name, {
                "name": skill_sheet.name,
                "price": skill_sheet.price,
                "tags": getattr(skill_sheet, 'tags', []),
                "version": getattr(skill_sheet, 'version', ''),
            })
            self.refresh_node_meta()  # v0.29.0: skills_count changed

            peers = self.storage.get_peers()
            targets = random.sample(peers, min(self._gossip_fanout, len(peers)))
            for peer in targets:
                asyncio.create_task(self._send_to_peer(peer, msg))

            logger.info(f"Announced skill '{skill_sheet.name}' to {len(targets)} peers")
            return skill_sheet.name
        except ValidationError as e:
            logger.error(f"Skill validation failed: {e}")
            raise

    async def query(self, query_type: str, value: str, network_timeout: float = 3.0) -> List[Dict[str, Any]]:
        """Queries local storage and the network for a skill."""
        value_norm = value.lower()
        results = []

        # Compute current load for local results
        own_load = -1
        if self._task_slots > 0:
            own_load = min(10, int((self._active_workers / self._task_slots) * 10))

        if query_type == "all":
            results = self.storage.query_all_active_skills()
            for skill_name, skill in self._own_skills.items():
                if self._skill_visibility.get(skill_name.lower(), "public") != "private":
                    results.append({
                        "node_id": self.node_info.node_id,
                        "host": self.node_info.host,
                        "port": self.node_info.port,
                        "skill_sheet": skill.to_dict(),
                        "_last_seen": time.time(),
                        "_announced_at": time.time(),
                        "_load": own_load,
                        "_provider_public_key": self._public_key_hex,
                        "sidecar_port": self._sidecar_port
                    })
        elif query_type == "name":
            results = self.storage.query_skills_by_name(value_norm)
            if value_norm in self._own_skills:
                if self._skill_visibility.get(value_norm.lower(), "public") != "private":
                    results.append({
                        "node_id": self.node_info.node_id,
                        "host": self.node_info.host,
                        "port": self.node_info.port,
                        "skill_sheet": self._own_skills[value_norm].to_dict(),
                        "_last_seen": time.time(),
                        "_announced_at": time.time(),
                        "_load": own_load,
                        "_provider_public_key": self._public_key_hex,
                        "sidecar_port": self._sidecar_port
                    })
        elif query_type == "tag":
            results = self.storage.query_skills_by_tag(value_norm)
            for skill_name, skill in self._own_skills.items():
                if value_norm in skill.tags:
                    if self._skill_visibility.get(skill_name.lower(), "public") != "private":
                        results.append({
                            "node_id": self.node_info.node_id,
                            "host": self.node_info.host,
                            "port": self.node_info.port,
                            "skill_sheet": skill.to_dict(),
                            "_last_seen": time.time(),
                            "_announced_at": time.time(),
                            "_load": own_load,
                            "_provider_public_key": self._public_key_hex,
                            "sidecar_port": self._sidecar_port
                        })
        elif query_type == "uri":
            results = self.storage.query_skills_by_uri(value_norm)
            for skill_name, skill in self._own_skills.items():
                if skill.uri and skill.uri.lower() == value_norm:
                    if self._skill_visibility.get(skill_name.lower(), "public") != "private":
                        results.append({
                            "node_id": self.node_info.node_id,
                            "host": self.node_info.host,
                            "port": self.node_info.port,
                            "skill_sheet": skill.to_dict(),
                            "_last_seen": time.time(),
                            "_announced_at": time.time(),
                            "_load": own_load,
                            "_provider_public_key": self._public_key_hex,
                            "sidecar_port": self._sidecar_port
                        })
        elif query_type == "uri_prefix":
            results = self.storage.query_skills_by_uri_prefix(value_norm)
            for skill_name, skill in self._own_skills.items():
                if skill.uri and skill.uri.lower().startswith(value_norm):
                    if self._skill_visibility.get(skill_name.lower(), "public") != "private":
                        results.append({
                            "node_id": self.node_info.node_id,
                            "host": self.node_info.host,
                            "port": self.node_info.port,
                            "skill_sheet": skill.to_dict(),
                            "_last_seen": time.time(),
                            "_announced_at": time.time(),
                            "_load": own_load,
                            "_provider_public_key": self._public_key_hex,
                            "sidecar_port": self._sidecar_port
                        })
        else:
            return []

        # KAD-assisted: ask plugins for additional providers
        try:
            plugin_results = await self._plugins.on_query(query_type, value_norm)
            for pr in plugin_results:
                results.append({
                    "node_id": pr["node_id"],
                    "host": pr["host"],
                    "port": pr["port"],
                    "sidecar_port": pr.get("sidecar_port", 0),
                    "skill_sheet": {"name": pr.get("skill_key", "unknown")},
                    "_source": "kad",
                })
            if plugin_results:
                logger.info(f"QUERY_KAD_HIT type={query_type} value={value_norm} results={len(plugin_results)}")
        except Exception as e:
            logger.warning(f"QUERY_KAD_ERR {e}")

        peers = self.storage.get_peers()
        if peers:
            msg = self._sign(Query(query_type=query_type, value=value_norm))
            tasks = [request_response(p.host, p.port, msg, timeout=network_timeout) for p in peers]
            network_responses = await asyncio.gather(*tasks)

            for resp in network_responses:
                if isinstance(resp, QueryResponse) and verify_message(resp):
                    results.extend(resp.results)

        # Deduplication by (node_id, skill_name) — normalized to catch case mismatches
        seen: Set[tuple] = set()
        unique_results = []
        for r in results:
            key = (r["node_id"], r["skill_sheet"]["name"].lower())
            if key not in seen:
                seen.add(key)
                unique_results.append(r)
        
        # Rank results
        unique_results = self._rank_results(unique_results)

        # C-01: Apply group-based discovery filters (require/prefer/exclude)
        unique_results = self._filter_providers_by_group(unique_results)

        # Record demand for zero-result queries
        if not unique_results:
            await self._enqueue_write(self.storage.record_demand, query_type, value_norm)

        return unique_results

    def _rank_results(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Ranks query results by liveness, freshness, bilateral balance, and availability (load)."""
        if len(results) <= 1:
            for r in results:
                for key in list(r.keys()):
                    if key.startswith("_"):
                        del r[key]
            return results

        now = time.time()

        # Collect raw signals
        for r in results:
            r["_liveness"] = now - r.get("_last_seen", now)    # seconds since last seen (lower is better)
            r["_freshness"] = now - r.get("_announced_at", now) # seconds since announced (lower is better)
            
            # Use actual ledger balance [Phase 7 fix]
            pub_key = r.get("_provider_public_key", "")
            if pub_key:
                balance = self.storage.get_ledger_balance(pub_key)
                r["_balance"] = balance if balance is not None else 0.0
            else:
                r["_balance"] = 0.0

            # Load-based availability: lower load = higher availability
            peer_load = r.get("_load", -1)
            if peer_load >= 0:
                r["_availability"] = 1.0 - (peer_load / 10.0)
            else:
                r["_availability"] = 0.5  # Unknown load: neutral

        # Normalize each signal to 0.0-1.0 (higher is better)
        def normalize(values):
            if not values:
                return [0.0] * len(results)
            min_v, max_v = min(values), max(values)
            if max_v == min_v:
                return [1.0] * len(values)
            return [(max_v - v) / (max_v - min_v) for v in values]  # invert: lower raw = higher score

        liveness_scores = normalize([r["_liveness"] for r in results])
        freshness_scores = normalize([r["_freshness"] for r in results])

        # Balance: higher is better (don't invert)
        balance_vals = [r["_balance"] for r in results]
        min_b, max_b = min(balance_vals), max(balance_vals)
        if max_b == min_b:
            balance_scores = [1.0] * len(results)
        else:
            balance_scores = [(v - min_b) / (max_b - min_b) for v in balance_vals]

        availability_scores = [r["_availability"] for r in results]

        # Weighted sum: liveness 0.3, freshness 0.2, balance 0.2, availability 0.3
        for i, r in enumerate(results):
            r["_score"] = (liveness_scores[i] * 0.3 +
                           freshness_scores[i] * 0.2 +
                           balance_scores[i] * 0.2 +
                           availability_scores[i] * 0.3)

        # Sort descending by score, with node_id tie-breaking for determinism
        results.sort(key=lambda r: (-r.get("_score", 0.0), r.get("node_id", "")))

        # Strip internal fields
        for r in results:
            for key in list(r.keys()):
                if key.startswith("_"):
                    del r[key]

        return results

    async def deregister(self, skill_name: str):
        """Removes a skill locally and from the network."""
        skill_name = skill_name.lower()
        if skill_name in self._own_skills:
            del self._own_skills[skill_name]
            self.refresh_node_meta()  # v0.29.0: skills_count changed
            await self._enqueue_write(self.storage.remove_skill, skill_name, self.node_info.node_id)

            msg = self._sign(Deregister(
                node_id=self.node_info.node_id,
                skill_key=skill_name
            ))
            
            peers = self.storage.get_peers()
            targets = random.sample(peers, min(self._gossip_fanout, len(peers)))
            for peer in targets:
                asyncio.create_task(self._send_to_peer(peer, msg))
            
            logger.info(f"Deregistered skill '{skill_name}'")

    def _get_skill_max_concurrent(self, skill_name: str) -> int:
        """Return the max_concurrent setting for a skill (default 1)."""
        skill_name_lower = skill_name.lower()
        mc = getattr(self, "_skill_max_concurrent", {})
        if skill_name_lower in mc:
            return mc[skill_name_lower]
        # Fallback: read from config
        skill_cfg = self._config.get("skills", {}).get(skill_name, {})
        if isinstance(skill_cfg, dict):
            try:
                return max(1, int(skill_cfg.get("max_concurrent", 1)))
            except (ValueError, TypeError):
                return 1
        return 1

    def register_handler(self, skill_name: str, handler_fn: Callable, slow: bool = False):
        """Registers a handler for a skill."""
        self._handlers[skill_name.lower()] = (handler_fn, slow)
        logger.info(f"Registered {'slow' if slow else 'fast'} handler for skill '{skill_name}'")

    async def register_system_skills(self, config: dict):
        """Register all system skills based on config.

        Handles the full sequence for each system skill: import, set_node,
        register_handler, announce, set visibility. Custom serve scripts
        call this one method instead of replicating 15 lines per skill.
        """
        # knarr-mail
        mail_cfg = config.get("mail", {})
        if mail_cfg.get("accept_from", "all") != "none":
            from ..mail.handler import handle as mail_handle, set_node as mail_set_node
            mail_set_node(self)
            self.register_handler("knarr-mail", mail_handle)
            await self.announce({
                "name": "knarr-mail",
                "version": "1.0.0",
                "description": "Signed agent-to-agent messaging. Send, poll, and acknowledge messages.",
                "tags": ["system", "messaging"],
                "input_schema": {},
                "output_schema": {},
                "price": float(mail_cfg.get("price", 1.0)),
                "max_input_size": 65536,
            })
            self._skill_visibility["knarr-mail"] = "public"
            logger.info("System skill registered: knarr-mail")

        # knarr-static
        static_cfg = config.get("static", {})
        if static_cfg.get("enabled", True):
            from ..static.handler import handle as static_handle, set_node as static_set_node
            static_set_node(self)
            self.register_handler("knarr-static", static_handle)
            await self.announce({
                "name": "knarr-static",
                "version": "1.0.0",
                "description": "Deploy and manage static web frontends. Local-only.",
                "tags": ["system", "static"],
                "input_schema": {},
                "output_schema": {},
                "price": 0.0,
                "max_input_size": 65536,
            })
            self._skill_visibility["knarr-static"] = "private"
            logger.info("System skill registered: knarr-static")

    async def call_local(self, skill_name: str, input_data: Dict[str, Any],
                         timeout_ms: int = 30000) -> Dict[str, Any]:
        """Calls a local skill handler directly, bypassing network and policy.

        Returns the handler's output dict. Raises KeyError if skill not found,
        asyncio.TimeoutError if handler exceeds timeout_ms, or propagates any
        exception from the handler itself.
        """
        key = skill_name.lower()
        logger.debug(f"call_local: {skill_name} (timeout={timeout_ms}ms)")
        if key not in self._handlers:
            raise KeyError(f"No local handler for '{skill_name}'")
        handler_fn, _slow = self._handlers[key]

        # Price for execution log (local calls bypass billing, record 0)
        skill_price = 0.0

        # H21-prep: Job ID propagation
        job_id = input_data.get("_job_id") or str(uuid.uuid4())
        input_data = dict(input_data or {})
        input_data["_job_id"] = job_id

        input_data = self._inject_secrets(key, input_data)
        # Inject caller identity as self (local call)
        input_data["_caller_node_id"] = self.node_info.node_id
        input_data["_node_encrypt"] = self.encrypt_for_peer
        input_data["_node_decrypt"] = self.decrypt_from_peer
        input_data["_send_mail"] = self._sync.enqueue

        # H22: Task Recording (strip non-serializable hooks for storage)
        task = Task(
            task_id=job_id, skill_name=skill_name,
            requester_node_id=self.node_info.node_id,
            provider_node_id=self.node_info.node_id,
            status="running",
            input_data={k: v for k, v in input_data.items() if not callable(v)},
            created_at=time.time(), updated_at=time.time(),
            timeout_ms=timeout_ms
        )
        await self._enqueue_write(self.storage.insert_task, task)

        # Compute input hash for log (strip non-serializable injected hooks)
        serializable = {k: v for k, v in input_data.items() if not callable(v)}
        canonical = json.dumps(serializable, sort_keys=True, separators=(',', ':'))
        input_hash = hashlib.sha256(canonical.encode()).hexdigest()

        # Capture asset_hash from input if any
        asset_hash = None
        for v in input_data.values():
            if isinstance(v, str) and v.startswith("knarr-asset://"):
                asset_hash = v[len("knarr-asset://"):]
                break

        # Check if handler accepts TaskContext
        import inspect
        ctx = TaskContext(self._asset_dir) if self._asset_dir else TaskContext("")
        handler_accepts_ctx = False
        try:
            sig = inspect.signature(handler_fn)
            handler_accepts_ctx = len(sig.parameters) >= 2
        except (ValueError, TypeError):
            pass

        start_time = time.time()
        loop = asyncio.get_running_loop()
        try:
            if asyncio.iscoroutinefunction(handler_fn):
                # Run async handlers in executor with their own event loop
                # so blocking I/O inside doesn't stall the main loop
                def _run_async():
                    tloop = asyncio.new_event_loop()
                    try:
                        if handler_accepts_ctx:
                            return tloop.run_until_complete(handler_fn(input_data, ctx))
                        else:
                            return tloop.run_until_complete(handler_fn(input_data))
                    finally:
                        tloop.close()
                coro = loop.run_in_executor(self._handler_pool, _run_async)
            else:
                if handler_accepts_ctx:
                    coro = loop.run_in_executor(self._handler_pool, handler_fn, input_data, ctx)
                else:
                    coro = loop.run_in_executor(self._handler_pool, handler_fn, input_data)

            result = await asyncio.wait_for(coro, timeout=timeout_ms / 1000)

            wall_ms = int((time.time() - start_time) * 1000)
            input_size = len(json.dumps(serializable).encode())
            # V013-006: Telemetry is best-effort — don't fail completed work on DB errors
            try:
                await self._enqueue_write(
                    self.storage.update_task_status, job_id, "completed",
                    result, None, input_size, wall_ms
                )
                await self._enqueue_write(
                    self.storage.log_execution,
                    job_id, skill_name, self.node_info.node_id, "completed", wall_ms, input_hash, asset_hash, None, skill_price, ""
                )
            except Exception as tel_err:
                logger.warning(f"Telemetry write failed for {job_id}: {tel_err}")
            return result

        except Exception as e:
            wall_ms = int((time.time() - start_time) * 1000)
            err_msg = str(e)
            if isinstance(e, asyncio.TimeoutError):
                err_msg = f"Handler exceeded {timeout_ms/1000}s timeout"
                if ctx:
                    ctx.cancelled.set()
                if not asyncio.iscoroutinefunction(handler_fn):
                    logger.warning(
                        f"ORPHAN_HANDLER skill={skill_name} job={job_id[:8]} "
                        f"timeout={timeout_ms/1000}s — thread may still be running"
                    )

            await self._enqueue_write(
                self.storage.update_task_status, job_id, "failed",
                None, {"code": "HANDLER_ERROR", "message": err_msg}
            )
            await self._enqueue_write(
                self.storage.log_execution,
                job_id, skill_name, self.node_info.node_id, "failed", wall_ms, input_hash, asset_hash, err_msg, skill_price, ""
            )
            raise

    async def request_task(
        self, provider_node_id: str, provider_host: str, provider_port: int,
        skill_name: str, input_data: Dict[str, Any], timeout_ms: int = 30000,
        skill_price: float = 1.0
    ) -> TaskResult:
        """Requests a task execution from a provider."""
        task_id = str(uuid.uuid4())
        task = Task(
            task_id=task_id,
            skill_name=skill_name,
            requester_node_id=self.node_info.node_id,
            provider_node_id=provider_node_id,
            status="submitted",
            input_data=input_data,
            created_at=time.time(),
            updated_at=time.time(),
            timeout_ms=timeout_ms
        )
        await self._enqueue_write(self.storage.insert_task, task)
        
        req = self._sign(TaskRequest(
            task_id=task_id,
            requester_node_id=self.node_info.node_id,
            requester_host=self.node_info.host,
            requester_port=self.node_info.port,
            skill_name=skill_name,
            input_data=input_data,
            timeout_ms=timeout_ms
        ))
        
        self._task_events[task_id] = asyncio.Event()
        self._task_expected_provider[task_id] = ""  # will be set from provider's response key

        try:
            resp = await request_response(provider_host, provider_port, req, timeout=timeout_ms/1000.0)
            
            if isinstance(resp, TaskResult) and verify_message(resp):
                await self._enqueue_write(
                    self.storage.update_task_status,
                    task_id, resp.status,
                    resp.output_data if resp.status == "completed" else None,
                    resp.error if resp.status == "failed" else None,
                    None, None,
                    resp.public_key
                )
                if resp.status == "completed":
                    _nid = hashlib.sha256(bytes.fromhex(resp.public_key)).hexdigest()
                    _trust = self._get_initial_trust(_nid)
                    await self._enqueue_write(self.storage.get_or_create_ledger_entry, resp.public_key, self.policy.initial_credit, _trust)
                    await self._enqueue_write(self.storage.update_ledger_consumer, resp.public_key, skill_price)
                    # E3: credit.change fires after ledger update (sync path)
                    if self.bus:
                        self.bus.emit(
                            "credit.change",
                            direction="consumer",
                            counterparty=resp.public_key,
                            amount=skill_price,
                            reference=task_id,
                            identity=resp.public_key,
                        )
                    if resp.receipt:
                        await self._enqueue_write(self.storage.store_receipt, task_id, resp.receipt)
                    
                    # S-023: Sync path receipt parity — generate receipt and credit note like async path
                    try:
                        from ..commerce.receipts import create_credit_note as _create_credit_note
                        import hashlib as _hl
                        output_hash = hashlib.sha256(
                            json.dumps(resp.output_data, sort_keys=True, separators=(',', ':')).encode()
                        ).hexdigest() if resp.output_data else ""
                        receipt_json = self._sign_receipt(
                            task_id=task_id, skill_name=skill_name,
                            consumer_node_id=self.node_info.node_id, credits_charged=skill_price,
                            input_hash="", output_hash=output_hash, wall_ms=0,
                            price_breakdown_json=None
                        )
                        await self._enqueue_write(self.storage.store_receipt, task_id, receipt_json)
                        
                        credit_note_json = _create_credit_note(
                            note_type="debit" if skill_price > 0 else "zero",
                            amount=float(skill_price),
                            issuer=self._public_key_hex,
                            recipient=resp.public_key,
                            reference=task_id,
                            description=f"skill:{skill_name} execution",
                            signing_key=self._signing_key,
                        )
                        await self._enqueue_write(
                            self.storage.store_credit_note,
                            resp.public_key, task_id, credit_note_json
                        )
                        # FIX-04: Write to receipt_log via centralized helper (replaces uuid/base64 path)
                        _note_type_sync = "debit" if skill_price > 0 else "zero"
                        self._write_receipt(
                            document_type="execution_receipt",
                            payload={
                                "provider": self.node_info.node_id,
                                "caller": resp.public_key,
                                "skill_uri": f"knarr:///{skill_name}",
                                "order_ref": task_id,
                                "execution": {
                                    "status": "completed",
                                    "duration_ms": 0,
                                    "input_hash": None,
                                    "output_hash": f"sha256:{output_hash}" if output_hash else None,
                                    "error": None,
                                },
                                "settlement": {
                                    "credit_note_ref": None,
                                    "amount": float(skill_price),
                                    "currency": "credits",
                                },
                            },
                            counterparty=resp.public_key,
                            order_ref=task_id,
                            proof_purpose="assertion",
                            sign=True,
                        )
                        self._write_receipt(
                            document_type="credit_note",
                            payload={
                                "note_type": _note_type_sync,
                                "amount": float(skill_price),
                                "currency": "credits",
                                "issuer": self.node_info.node_id,
                                "recipient": resp.public_key,
                                "reference": task_id,
                                "description": f"skill:{skill_name} execution",
                            },
                            counterparty=resp.public_key,
                            order_ref=task_id,
                            proof_purpose="assertion",
                            sign=True,
                        )
                        # E3: receipt.issued fires AFTER storage (sync path parity)
                        if self.bus:
                            self.bus.emit(
                                "receipt.issued",
                                note_type=_note_type_sync,
                                counterparty=resp.public_key,
                                amount=skill_price,
                                reference=task_id,
                                identity=resp.public_key,
                            )
                    except Exception as _cn_err:
                        logger.warning(f"CREDIT_NOTE_ISSUE_FAIL (sync path) job={task_id[:8]}: {_cn_err}")
                return resp

            if isinstance(resp, TaskStatus) and verify_message(resp):
                # SA6-01: Bind expected provider identity for async result
                self._task_expected_provider[task_id] = resp.public_key

                if resp.status == "rejected":
                    error = {"code": "REJECTED", "message": resp.reason}
                    await self._enqueue_write(self.storage.update_task_status, task_id, "rejected", None, error)
                    return TaskResult(task_id=task_id, status="failed", error=error)

                if resp.status == "queued" and resp.position > 0:
                    logger.info(f"Task {task_id[:8]} queued at position {resp.position}")
                await self._enqueue_write(self.storage.update_task_status, task_id, resp.status)
                try:
                    await asyncio.wait_for(self._task_events[task_id].wait(), timeout=timeout_ms/1000.0)
                    result = self._task_results.get(task_id)
                    if result:
                        if result.status == "completed":
                            await self._enqueue_write(
                                self.storage.update_task_status,
                                task_id, result.status,
                                result.output_data,
                                None, None, None,
                                result.public_key
                            )
                            _nid2 = hashlib.sha256(bytes.fromhex(result.public_key)).hexdigest()
                            _trust2 = self._get_initial_trust(_nid2)
                            await self._enqueue_write(self.storage.get_or_create_ledger_entry, result.public_key, self.policy.initial_credit, _trust2)
                            await self._enqueue_write(self.storage.update_ledger_consumer, result.public_key, skill_price)
                            # E3: credit.change fires after ledger update (sync queued path)
                            if self.bus:
                                self.bus.emit(
                                    "credit.change",
                                    direction="consumer",
                                    counterparty=result.public_key,
                                    amount=skill_price,
                                    reference=task_id,
                                    identity=result.public_key,
                                )
                            
                            # S-023: Sync queued path receipt parity — generate receipt and credit note
                            try:
                                from ..commerce.receipts import create_credit_note as _create_credit_note
                                output_hash_q = hashlib.sha256(
                                    json.dumps(result.output_data, sort_keys=True, separators=(',', ':')).encode()
                                ).hexdigest() if result.output_data else ""
                                receipt_json_q = self._sign_receipt(
                                    task_id=task_id, skill_name=skill_name,
                                    consumer_node_id=self.node_info.node_id, credits_charged=skill_price,
                                    input_hash="", output_hash=output_hash_q, wall_ms=0,
                                    price_breakdown_json=None
                                )
                                await self._enqueue_write(self.storage.store_receipt, task_id, receipt_json_q)
                                
                                credit_note_json_q = _create_credit_note(
                                    note_type="debit" if skill_price > 0 else "zero",
                                    amount=float(skill_price),
                                    issuer=self._public_key_hex,
                                    recipient=result.public_key,
                                    reference=task_id,
                                    description=f"skill:{skill_name} execution",
                                    signing_key=self._signing_key,
                                )
                                await self._enqueue_write(
                                    self.storage.store_credit_note,
                                    result.public_key, task_id, credit_note_json_q
                                )
                                # Write to receipt_log (B1)
                                import uuid as _uuid
                                from datetime import datetime, timezone as _tz
                                receipt_id_q = f"exec_{_uuid.uuid4().hex[:12]}"
                                timestamp_q = datetime.now(_tz.utc).isoformat()
                                import base64 as _b64
                                canonical_q = json.dumps(json.loads(receipt_json_q), sort_keys=True, separators=(',', ':')).encode('utf-8')
                                sig_q = _b64.b64encode(self._signing_key.sign(canonical_q).signature).decode('ascii') if hasattr(self, '_signing_key') else None
                                self.storage.write_receipt(
                                    receipt_id=receipt_id_q,
                                    document_type="execution_receipt",
                                    timestamp=timestamp_q,
                                    identity=self._public_key_hex,
                                    counterparty=result.public_key,
                                    order_ref=task_id,
                                    proof_purpose="assertion",
                                    payload_json=receipt_json_q,
                                    signature=sig_q
                                )
                                if self.bus:
                                    self.bus.emit(
                                        "receipt.issued",
                                        note_type="debit" if skill_price > 0 else "zero",
                                        counterparty=result.public_key,
                                        amount=skill_price,
                                        reference=task_id,
                                        identity=result.public_key,
                                    )
                            except Exception as _cn_err_q:
                                logger.warning(f"CREDIT_NOTE_ISSUE_FAIL (sync queued) job={task_id[:8]}: {_cn_err_q}")
                        else:
                            await self._enqueue_write(
                                self.storage.update_task_status,
                                task_id, result.status,
                                None, result.error,
                                None, None,
                                result.public_key
                            )
                        return result
                    else:
                        raise Exception("Task result missing after event set")
                except asyncio.TimeoutError:
                    error = {"code": "TIMEOUT", "message": "Task timed out waiting for async result"}
                    await self._enqueue_write(self.storage.update_task_status, task_id, "failed", None, error)
                    return TaskResult(task_id=task_id, status="failed", error=error)
            
            error = {"code": "NETWORK_ERROR", "message": "Provider returned invalid response or connection failed"}
            await self._enqueue_write(self.storage.update_task_status, task_id, "failed", None, error)
            return TaskResult(task_id=task_id, status="failed", error=error)
            
        finally:
            self._task_events.pop(task_id, None)
            self._task_results.pop(task_id, None)
            self._task_expected_provider.pop(task_id, None)

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handles incoming TCP connections with signature verification and concurrency limits."""
        peer_ip = writer.get_extra_info("peername", ("?", 0))[0]

        # V015: Pre-auth plugin gate (cheapest rejection point)
        if not await self._plugins.on_connect(peer_ip):
            # v0.33.0: firewall.blocked
            if self.bus:
                self.bus.emit("firewall.blocked", from_node="unknown", msg_type="connect", reason="on_connect_rejected", identity=self.node_info.node_id)
            writer.close()
            await writer.wait_closed()
            return

        # SA-02: Reject at accept level — close immediately if over limit (FDs already allocated)
        if self._active_connections >= MAX_CONCURRENT_CONNECTIONS:
            writer.close()
            await writer.wait_closed()
            return
        self._active_connections += 1
        try:
            # S-027: HTTP-to-TCP port confusion fix — detect HTTP verbs before message parsing.
            # HTTP GET/POST etc. to protocol port (9030) would be parsed as massive length prefix,
            # triggering OOM-scale buffer allocation. Check before receive_message() reads 4-byte length.
            http_verbs = (b'GET ', b'POST', b'PUT ', b'DELE', b'HEAD', b'OPTI', b'PATC')
            peek_bytes = await asyncio.wait_for(reader.read(4), timeout=2.0)  # L-03: reduced from 5s
            if peek_bytes and peek_bytes[:4].upper() in http_verbs:
                logger.warning(f"HTTP_REJECTED: peer_ip={peer_ip} attempted HTTP to protocol port")
                if self.bus:
                    self.bus.emit("firewall.blocked", from_node="unknown", msg_type="HTTP", reason="http_to_protocol_port", identity=self.node_info.node_id)
                writer.close()
                await writer.wait_closed()
                return
            # Prepend peeked bytes back to stream for normal message parsing.
            # PRIVATE API: asyncio.StreamReader._buffer — verify on Python upgrades (L-01).
            # Short/empty reads safely fall through — no verb match, message parse will reject (L-02).
            # Safe: knarr max msg size << 0x47455420; no false-positive overlap with HTTP verbs (L-04).
            try:
                reader._buffer[0:0] = peek_bytes
            except AttributeError:
                logger.warning("HTTP_PEEK: reader has no _buffer, closing connection")
                writer.close()
                await writer.wait_closed()
                return

            signer_id = ""  # FIX-02: init before loop; set properly after verify_node_id
            # Message loop: handle multiple messages per persistent connection.
            # Connection pooling on the client side keeps connections open for reuse.
            # The loop breaks on: EOF (client closed), timeout (idle), or error.
            # SERVER_IDLE_TIMEOUT must be > HEARTBEAT_SILENCE_THRESHOLD so pooled
            # heartbeat connections aren't closed before the next heartbeat arrives.
            while True:
                try:
                    msg = await asyncio.wait_for(receive_message(reader), timeout=SERVER_IDLE_TIMEOUT)
                    if not msg:
                        break  # EOF — client closed connection

                    if not verify_message(msg):
                        logger.warning(f"Dropping message with invalid signature: type={msg.type}")
                        # v0.33.0: security.signature_invalid
                        if self.bus:
                            self.bus.emit("security.signature_invalid", msg_type=msg.type, from_ip=(peer_ip or "")[:20], identity=signer_id if signer_id else peer_ip or "unknown")
                        break

                    if not verify_node_id(msg):
                        logger.warning(f"Dropping message with mismatched node_id: type={msg.type}")
                        # v0.33.0: security.identity_mismatch
                        if self.bus:
                            claimed = getattr(msg, 'node_id', '') or getattr(msg, 'sender_node_id', '') or ''
                            self.bus.emit("security.identity_mismatch", msg_type=msg.type, from_ip=(peer_ip or "")[:20], claimed_id=claimed[:16], identity=signer_id if signer_id else peer_ip or "unknown")
                        break

                    # SA-ML6: Derive sender identity from signer (public_key), not self-asserted fields.
                    # This prevents requester_node_id spoofing in TaskRequest.
                    signer_id = hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest() if msg.public_key else ''

                    # Record peer activity — any verified message proves liveness
                    if signer_id:
                        self._peer_last_activity[signer_id] = time.monotonic()
                        logger.debug(f"IMPLICIT_HB from={signer_id[:16]} type={msg.type}")
                        # v0.17.4: Push pending mail on any inbound activity (not just heartbeats)
                        # Without this, frequent announces reset silence timer, preventing outbound
                        # heartbeats, which were the only trigger for mail push.
                        peer_info = next((p for p in self.storage.get_peers() if p.node_id == signer_id), None)
                        if peer_info:
                            h, p = self.resolve_peer(peer_info.node_id, peer_info.host, peer_info.port)
                            asyncio.create_task(self._sync.push_to_peer(peer_info.node_id, h, p))

                    # V015: Plugin inbound gate
                    if not await self._plugins.on_inbound(msg, peer_ip):
                        # v0.33.0: firewall.blocked
                        if self.bus:
                            self.bus.emit("firewall.blocked", from_node=signer_id or peer_ip, msg_type=msg.type, reason="on_inbound_rejected", identity=signer_id or peer_ip or "unknown")
                        continue  # Plugin suppressed — skip but keep connection open

                    # v0.17.0: Auto-populate address book cached tier (V17-004: after plugin gate)
                    if signer_id:
                        await self._enqueue_write_proto(
                            self.storage.upsert_address,
                            signer_id, "cached", None,
                            peer_ip, getattr(msg, 'port', 0),
                            getattr(msg, 'sidecar_port', 0)
                        )

                    response = await self._process_message(msg, peer_ip=peer_ip)
                    if response:
                        await send_message(writer, response)
                except asyncio.TimeoutError:
                    break  # Idle timeout — close connection
                except ProtocolError as e:
                    # v0.29.1: Log peer IP for oversized/malformed messages
                    logger.warning(f"PROTOCOL_ERR from={peer_ip}: {e}")
                    break
                except Exception as e:
                    logger.error(f"Error handling connection: {e}")
                    break
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        finally:
            self._active_connections -= 1

    def _validate_peer_fields(self, node_id: str, host: str, port: int) -> bool:
        """Validates peer fields from network messages."""
        if not node_id or len(node_id) > 128:
            return False
        if not host or len(host) > 256:
            return False
        if not isinstance(port, int) or port < 1 or port > 65535:
            return False
        return True

    async def _process_message(self, msg: Message, peer_ip: str = "") -> Optional[Message]:
        """Processes a received message and returns a signed response."""
        # L-06: reject messages with malformed public_key (odd-length hex crashes bytes.fromhex)
        pk = getattr(msg, "public_key", None)
        if pk and (len(pk) % 2 != 0 or not all(c in "0123456789abcdefABCDEF" for c in pk)):
            logger.warning(f"MALFORMED_PUBKEY len={len(pk)} msg_type={type(msg).__name__}")
            return self._sign(Ack(status="error", error_detail="Malformed public key", msg_id=getattr(msg, "msg_id", "")))

        if isinstance(msg, JoinRequest):
            if not self._validate_peer_fields(msg.node_id, msg.host, msg.port):
                return self._sign(Ack(status="error", error_detail="Invalid peer fields", msg_id=msg.msg_id))
            if not msg.ephemeral:
                peer = NodeInfo(node_id=msg.node_id, host=msg.host, port=msg.port)
                await self._enqueue_write_proto(self.storage.upsert_peer, peer)
                # v0.33.0: peer.added
                if self.bus:
                    peer_count = len(self.storage.get_peers())
                    self.bus.emit("peer.added", node_id=msg.node_id, host=msg.host, port=msg.port, peer_count=peer_count, identity=self.node_info.node_id)
            peers = self.storage.get_peers()
            peers.append(self.node_info)
            return self._sign(JoinResponse(peers=[asdict(p) for p in peers]))

        elif isinstance(msg, Announce):
            try:
                skill_sheet = validate_skill_sheet(msg.skill_sheet)
                skill_ttl = self._get_skill_ttl()
                
                # store peer's encryption key if provided (verify derivation to prevent MITM)
                if msg.encryption_key:
                    try:
                        from nacl.signing import VerifyKey
                        expected = VerifyKey(bytes.fromhex(msg.public_key)).to_curve25519_public_key().encode().hex()
                        if msg.encryption_key == expected:
                            await self._enqueue_write(
                                self.storage.update_peer_encryption_key,
                                msg.node_id, msg.encryption_key
                            )
                        else:
                            logger.warning(f"Announce from {msg.node_id[:16]}: encryption_key mismatch, ignoring")
                    except Exception as e:
                        logger.warning(f"Failed to verify encryption_key derivation: {e}")

                # Store peer's wallet address if provided (verify derivation to prevent MITM)
                if msg.wallet:
                    try:
                        from ..core.wallet import b58encode
                        from nacl.signing import VerifyKey
                        expected = b58encode(VerifyKey(bytes.fromhex(msg.public_key)).encode())
                        if msg.wallet == expected:
                            await self._enqueue_write(
                                self.storage.update_peer_wallet,
                                msg.node_id, msg.wallet
                            )
                        else:
                            logger.warning(f"Announce from {msg.node_id[:16]}: wallet mismatch, ignoring")
                    except Exception as e:
                        logger.warning(f"Failed to verify wallet derivation: {e}")

                # v0.26.0: Store jurisdiction (no verification needed — informational)
                if msg.jurisdiction:
                    await self._enqueue_write(
                        self.storage.update_peer_jurisdiction,
                        msg.node_id, msg.jurisdiction
                    )

                await self._enqueue_write(
                    self.storage.upsert_skill,
                    msg.skill_key, msg.node_id, skill_sheet,
                    skill_ttl, # ttl
                    False, # is_own
                    msg.public_key,
                    msg.signature,
                    msg.msg_id,
                    msg.sidecar_port,
                    msg.provider_host,
                    msg.provider_port,
                )
                logger.debug(f"SKILL_STORE key={msg.skill_key} from={msg.node_id[:16]} ttl={skill_ttl} hops={msg.hops}")

                # Gossip forward if hops remain
                dedup_key = (msg.skill_key, msg.node_id, msg.msg_id)
                if dedup_key not in self._seen_messages and msg.hops < self._get_announce_hops():
                    self._seen_messages.add(dedup_key)
                    if len(self._seen_messages) > MAX_DEDUP_SET_SIZE:
                        self._seen_messages = set(list(self._seen_messages)[MAX_DEDUP_SET_SIZE // 2:])

                    forwarded = Announce(
                        msg_id=msg.msg_id,
                        node_id=msg.node_id,
                        skill_key=msg.skill_key,
                        skill_sheet=msg.skill_sheet,
                        hops=msg.hops + 1,
                        public_key=msg.public_key,
                        signature=msg.signature,
                        sidecar_port=msg.sidecar_port,
                        encryption_key=msg.encryption_key,
                        wallet=msg.wallet,
                        provider_host=msg.provider_host,
                        provider_port=msg.provider_port,
                        jurisdiction=msg.jurisdiction,
                    )
                    peers = self.storage.get_peers()
                    eligible = [p for p in peers if p.node_id != msg.node_id]
                    targets = random.sample(eligible, min(self._gossip_fanout, len(eligible)))
                    for peer in targets:
                        asyncio.create_task(self._send_to_peer(peer, forwarded))

                return self._sign(Ack(status="ok", msg_id=msg.msg_id))
            except ValidationError as e:
                return self._sign(Ack(status="error", error_detail=str(e), msg_id=msg.msg_id))

        elif isinstance(msg, Query):
            query_val = msg.value.lower()
            results = []
            
            # Compute current load for own results
            own_load = -1
            if self._task_slots > 0:
                own_load = min(10, int((self._active_workers / self._task_slots) * 10))

            if msg.query_type == "all":
                results = self.storage.query_all_active_skills()
                for skill_name, skill in self._own_skills.items():
                    if self._skill_visibility.get(skill_name.lower(), "public") != "private":
                        results.append({
                            "node_id": self.node_info.node_id,
                            "host": self.node_info.host,
                            "port": self.node_info.port,
                            "skill_sheet": skill.to_dict(),
                            "_load": own_load,
                            "_provider_public_key": self._public_key_hex,
                            "sidecar_port": self._sidecar_port
                        })
            elif msg.query_type == "name":
                results = self.storage.query_skills_by_name(query_val)
                if query_val in self._own_skills:
                    if self._skill_visibility.get(query_val.lower(), "public") != "private":
                        results.append({
                            "node_id": self.node_info.node_id,
                            "host": self.node_info.host,
                            "port": self.node_info.port,
                            "skill_sheet": self._own_skills[query_val].to_dict(),
                            "_load": own_load,
                            "_provider_public_key": self._public_key_hex,
                            "sidecar_port": self._sidecar_port
                        })
            elif msg.query_type == "tag":
                results = self.storage.query_skills_by_tag(query_val)
                for skill_name, skill in self._own_skills.items():
                    if query_val in skill.tags:
                        if self._skill_visibility.get(skill_name.lower(), "public") != "private":
                            results.append({
                                "node_id": self.node_info.node_id,
                                "host": self.node_info.host,
                                "port": self.node_info.port,
                                "skill_sheet": skill.to_dict(),
                                "_load": own_load,
                                "_provider_public_key": self._public_key_hex,
                                "sidecar_port": self._sidecar_port
                            })
            elif msg.query_type == "uri":
                results = self.storage.query_skills_by_uri(query_val)
                for skill_name, skill in self._own_skills.items():
                    if skill.uri and skill.uri.lower() == query_val:
                        if self._skill_visibility.get(skill_name.lower(), "public") != "private":
                            results.append({
                                "node_id": self.node_info.node_id,
                                "host": self.node_info.host,
                                "port": self.node_info.port,
                                "skill_sheet": skill.to_dict(),
                                "_load": own_load,
                                "_provider_public_key": self._public_key_hex,
                                "sidecar_port": self._sidecar_port
                            })
            elif msg.query_type == "uri_prefix":
                results = self.storage.query_skills_by_uri_prefix(query_val)
                for skill_name, skill in self._own_skills.items():
                    if skill.uri and skill.uri.lower().startswith(query_val):
                        if self._skill_visibility.get(skill_name.lower(), "public") != "private":
                            results.append({
                                "node_id": self.node_info.node_id,
                                "host": self.node_info.host,
                                "port": self.node_info.port,
                                "skill_sheet": skill.to_dict(),
                                "_load": own_load,
                                "_provider_public_key": self._public_key_hex,
                                "sidecar_port": self._sidecar_port
                            })

            # KAD-assisted: ask plugins for additional providers
            try:
                plugin_results = await self._plugins.on_query(msg.query_type, query_val)
                for pr in plugin_results:
                    results.append({
                        "node_id": pr["node_id"],
                        "host": pr["host"],
                        "port": pr["port"],
                        "sidecar_port": pr.get("sidecar_port", 0),
                        "skill_sheet": {"name": pr.get("skill_key", "unknown")},
                        "_source": "kad",
                    })
                if plugin_results:
                    logger.info(f"QUERY_KAD_HIT type={msg.query_type} value={query_val} results={len(plugin_results)}")
            except Exception as e:
                logger.warning(f"QUERY_KAD_ERR {e}")

            return self._sign(QueryResponse(results=results))

        elif isinstance(msg, Deregister):
            await self._enqueue_write(self.storage.remove_skill, msg.skill_key, msg.node_id)

            # Gossip forward [L-02]
            dedup_key = (msg.skill_key, msg.node_id, msg.msg_id)
            if dedup_key not in self._seen_messages and msg.hops < self._get_announce_hops():
                self._seen_messages.add(dedup_key)
                if len(self._seen_messages) > MAX_DEDUP_SET_SIZE:
                    self._seen_messages = set(list(self._seen_messages)[MAX_DEDUP_SET_SIZE // 2:])

                forwarded = Deregister(
                    msg_id=msg.msg_id,
                    node_id=msg.node_id,
                    skill_key=msg.skill_key,
                    hops=msg.hops + 1,
                    public_key=msg.public_key,
                    signature=msg.signature
                )
                peers = self.storage.get_peers()
                eligible = [p for p in peers if p.node_id != msg.node_id]
                targets = random.sample(eligible, min(self._gossip_fanout, len(eligible)))
                for peer in targets:
                    asyncio.create_task(self._send_to_peer(peer, forwarded))

            return self._sign(Ack(status="ok", msg_id=msg.msg_id))

        elif isinstance(msg, Heartbeat):
            # SA-FW2: Throttle heartbeat downstream work — max once per 5s per peer
            last_hb = self._peer_last_hb_work.get(msg.node_id, 0)
            now_mono = time.monotonic()
            if now_mono - last_hb >= 5.0:
                self._peer_last_hb_work[msg.node_id] = now_mono
                await self._enqueue_write(self.storage.touch_peer, msg.node_id)
                logger.debug(f"HB_RECV from={msg.node_id[:16]} — DB touched")

                # v0.17.0: If they pinged us, try to push any mail we have for them
                peer_info = next((p for p in self.storage.get_peers() if p.node_id == msg.node_id), None)
                if peer_info:
                    h, p = self.resolve_peer(peer_info.node_id, peer_info.host, peer_info.port)
                    asyncio.create_task(self._sync.push_to_peer(peer_info.node_id, h, p))

            return self._sign(Heartbeat(
                node_id=self.node_info.node_id,
                timestamp=time.time(),
                version=__version__,
                min_protocol_version=self._min_protocol_version,
            ))
            
        elif isinstance(msg, TaskRequest):
            return await self._handle_task_request(msg)
            
        elif isinstance(msg, TaskResult):
            if msg.task_id in self._task_events:
                # SA6-01: Verify result comes from expected provider
                expected_key = self._task_expected_provider.get(msg.task_id, "")
                if expected_key and msg.public_key != expected_key:
                    logger.warning(f"Rejecting TaskResult for {msg.task_id[:8]} from unexpected signer")
                    return self._sign(Ack(status="error", error_detail="Signer mismatch", msg_id=msg.msg_id))
                self._task_results[msg.task_id] = msg
                self._task_events[msg.task_id].set()
                await self._enqueue_write(
                    self.storage.update_task_status,
                    msg.task_id, msg.status,
                    msg.output_data if msg.status == "completed" else None,
                    msg.error if msg.status == "failed" else None,
                    None, None,
                    msg.public_key
                )
                
                # Consumer-side ledger update happens in request_task after event fires
                return self._sign(Ack(status="ok", msg_id=msg.msg_id))
            return self._sign(Ack(status="error", error_detail="Task ID not found", msg_id=msg.msg_id))

        elif isinstance(msg, SyncRequest):
            all_skills = self.storage.get_all_skills(since=msg.since)
            # Filter out own private skills
            own_private = {name for name, vis in self._skill_visibility.items() if vis == "private"}
            sync_skills = [s for s in all_skills if s["skill_key"] not in own_private]
            return self._sign(SyncResponse(skills=sync_skills))

        elif isinstance(msg, MailSync):
            # V17-001: Verify sender_node_id matches public_key
            expected_id = hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest()
            if msg.sender_node_id != expected_id:
                logger.warning(f"MailSync sender_node_id mismatch from {peer_ip}")
                return self._sign(Ack(status="error", error_detail="Identity mismatch", msg_id=msg.msg_id))
            return await self._sync.handle_mail_sync(msg, peer_ip)

        elif isinstance(msg, MailAck):
            # V17-001: Verify sender_node_id matches public_key
            expected_id = hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest()
            if msg.sender_node_id != expected_id:
                logger.warning(f"MailAck sender_node_id mismatch from {peer_ip}")
                return None
            await self._sync.handle_mail_ack(msg)
            return self._sign(Ack(status="ok", msg_id=msg.msg_id))

        elif isinstance(msg, MailPullReq):
            # v0.26.0: Tier 2 pull — verify requester identity
            expected_id = hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest()
            if msg.requester_node_id != expected_id:
                logger.warning(f"MailPullReq requester_node_id mismatch from {peer_ip}")
                return self._sign(Ack(status="error", error_detail="Identity mismatch", msg_id=msg.msg_id))
            resp = await self._sync.handle_mail_pull_req(msg, peer_ip)
            return self._sign(resp)

        elif isinstance(msg, MailPullAck):
            # v0.26.0: Tier 2 pull ACK — verify requester identity
            expected_id = hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest()
            if msg.requester_node_id != expected_id:
                logger.warning(f"MailPullAck requester_node_id mismatch from {peer_ip}")
                return None
            await self._sync.handle_mail_pull_ack(msg)
            return self._sign(Ack(status="ok", msg_id=msg.msg_id))

        elif isinstance(msg, PluginMessage):
            # Route to plugin chain — plugins handle via on_inbound.
            # Core does nothing with PluginMessage content.
            # If no plugin handled it (on_inbound returned True for all),
            # the message is silently dropped. This is correct behavior:
            # the remote node's plugin spoke to our plugin. If we don't
            # have the plugin, there's nothing to do.
            return None

        return None

    async def _maybe_send_tab_reminder(self, peer_public_key: str, balance: float, initial_credit: float, min_balance: float) -> None:
        """Send tab_reminder if peer's credit utilization exceeds threshold."""
        credit_range = initial_credit - min_balance
        if credit_range <= 0:
            return
        utilization = max(0.0, min(100.0, ((initial_credit - balance) / credit_range) * 100.0))

        threshold = self._get_settlement_config().get("tab_reminder_threshold", 80.0)
        if utilization < threshold:
            return

        # v0.33.0: credit.warning — soft limit breach
        if self.bus:
            self.bus.emit("credit.warning", counterparty=peer_public_key, utilization=utilization, threshold=threshold, identity=peer_public_key)

        if not self.storage.should_send_tab_reminder(peer_public_key, cooldown=3600):
            return

        peer_node_id = self.storage.get_node_id_for_public_key(peer_public_key)
        if not peer_node_id:
            return

        reminder = {
            "type": "knarr/commerce/tab_reminder",
            "current_balance": round(balance, 2),
            "credit_limit": round(credit_range, 2),
            "utilization_pct": round(utilization, 1),
            "timestamp": time.time(),
            "schema_version": "1.0",
        }
        # Fire-and-forget async enqueue
        asyncio.ensure_future(self._sync.enqueue(
            to_node=peer_node_id,
            msg_type="knarr/commerce/tab_reminder",
            body=reminder,
            system=True,
        ))

    async def _handle_task_request(self, msg: TaskRequest) -> Message:
        """Handles a task request from a consumer."""
        from .mcp_bridge import MCPTimeoutError

        # SA6-02: Replay guard — reject duplicate msg_ids
        if msg.msg_id in self._seen_task_requests:
            return self._sign(TaskResult(
                task_id=msg.task_id, status="failed",
                error={"code": "DUPLICATE_REQUEST", "message": "Request already processed"}
            ))
        self._seen_task_requests.add(msg.msg_id)
        if len(self._seen_task_requests) > MAX_TASK_DEDUP_SIZE:
            # Evict oldest half (set is unordered but this prevents unbounded growth)
            self._seen_task_requests = set(list(self._seen_task_requests)[MAX_TASK_DEDUP_SIZE // 2:])

        # Version gating: reject tasks when below minimum protocol version
        if self._version_gated:
            self._emit_task_rejected(msg.skill_name.lower(), msg.public_key, msg.task_id, "VERSION_GATED")
            return self._sign(TaskResult(
                task_id=msg.task_id, status="failed",
                error={"code": "VERSION_GATED", "message": f"Node version {__version__} below network minimum — update required"}
            ))

        # Auto-upgrade in progress: signal RETRY_AFTER
        if self._upgrading:
            return self._sign(TaskResult(
                task_id=msg.task_id, status="failed",
                error={"code": "RETRY_AFTER", "message": "Node upgrading", "retry_after_seconds": 60}
            ))

        skill_name = msg.skill_name.lower()
        logger.debug(f"TaskRequest: skill={skill_name} task={msg.task_id[:8]} from={msg.requester_node_id[:16]}")

        if skill_name not in self._handlers:
            self._emit_task_rejected(skill_name, msg.public_key, msg.task_id, "UNKNOWN_SKILL")
            return self._sign(TaskResult(
                task_id=msg.task_id,
                status="failed",
                error={"code": "UNKNOWN_SKILL", "message": f"No handler for skill '{skill_name}'"}
            ))
        
        handler_fn, slow = self._handlers[skill_name]
        skill_sheet = self._own_skills.get(skill_name)
        if not skill_sheet:
             return self._sign(TaskResult(
                task_id=msg.task_id, 
                status="failed", 
                error={"code": "UNKNOWN_SKILL", "message": "Skill sheet missing"}
            ))

        # v0.26.0: _healthcheck bypass — skip input_schema validation for health probes
        if not msg.input_data.get("_healthcheck"):
            error = validate_task_input(msg.input_data, skill_sheet.input_schema, skill_sheet.max_input_size)
            if error:
                return self._sign(TaskResult(task_id=msg.task_id, status="failed", error=error))

        # SA6-03: Validate callback address for slow-task result delivery
        if not self._validate_peer_fields(msg.requester_node_id or "unknown", msg.requester_host or "", msg.requester_port):
            return self._sign(TaskResult(
                task_id=msg.task_id, status="failed",
                error={"code": "INVALID_REQUEST", "message": "Invalid requester address fields"}
            ))

        # Visibility/Whitelist check — BEFORE policy to avoid ledger mutation for unauthorized callers [P6A-004]
        # Self-calls (e.g. cockpit dispatching slow local skills via TCP) bypass visibility
        is_self_call = msg.public_key == self._public_key_hex
        visibility = self._skill_visibility.get(skill_name, "public")
        if visibility == "private" and not is_self_call:
            logger.debug(f"ACCESS_DENIED: skill={skill_name} is private, from={msg.public_key[:16]}")
            self._emit_task_rejected(skill_name, msg.public_key, msg.task_id, "ACCESS_DENIED")
            return self._sign(TaskResult(
                task_id=msg.task_id,
                status="failed",
                error={"code": "ACCESS_DENIED",
                       "message": f"Skill '{skill_name}' is private"}
            ))
        if visibility == "whitelist" and not is_self_call:
            # Derive caller's node_id from their public key (not self-asserted msg.node_id)
            caller_node_id = hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest()
            allowed = self._skill_allowed_nodes.get(skill_name, [])
            if caller_node_id not in allowed:
                logger.debug(f"ACCESS_DENIED: skill={skill_name} from={caller_node_id[:16]}")
                self._emit_task_rejected(skill_name, msg.public_key, msg.task_id, "ACCESS_DENIED")
                return self._sign(TaskResult(
                    task_id=msg.task_id,
                    status="failed",
                    error={"code": "ACCESS_DENIED",
                           "message": f"Not authorized to execute skill '{skill_name}'. Your node_id: {caller_node_id}"}
                ))

        # Policy check — all writes through writer queue [R-01, CLAUDE-001]
        initial_credit, min_balance = self._resolve_policy(msg.public_key, skill_name)
        peer_nid = hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest()
        initial_trust = self._get_initial_trust(peer_nid)
        entry = await self._enqueue_write(
            self.storage.get_or_create_ledger_entry,
            msg.public_key, initial_credit, initial_trust
        )

        # v0.25.0: Commerce Tab Reminder
        await self._maybe_send_tab_reminder(msg.public_key, entry.balance, initial_credit, min_balance)

        # B1: Replace inline balance check with admission pipeline
        from ..commerce.admission_pipeline import run_admission, AdmissionContext

        # Query meter count for bounty decay and rate limiting
        meter = await self._enqueue_write(
            self.storage.meter_get, peer_nid, skill_name, ""
        )
        meter_count = meter["count"] if meter else 0
        
        # Runtime skill config comes from [skills."name"] in knarr.toml.
        skill_cfg = self._get_skill_runtime_config(skill_name)
        if not skill_cfg and skill_sheet and hasattr(skill_sheet, "config") and isinstance(skill_sheet.config, dict):
            skill_cfg = dict(skill_sheet.config)
        
        # Build admission context
        ctx = AdmissionContext(
            caller_key=msg.public_key,
            skill_name=skill_name,
            base_price=skill_sheet.price if skill_sheet else 1.0,
            balance=entry.balance,
            soft_limit=min_balance,  # from _resolve_policy
            hard_limit=min_balance,  # from _resolve_policy
            tit_for_tat=self.policy.tit_for_tat,
            peer_node_id=peer_nid,
            peer_groups=set(self._group_engine.get_groups(peer_nid)) if self._group_engine else set(),
            discount_rules=self._load_discount_rules(peer_nid, skill_name),
            cost_projection=self._get_cost_projection(skill_name),
            pricing_config=self._build_pricing_config(skill_name),
            prepaid_balance=getattr(entry, 'prepaid', 0.0),
            meter_count=meter_count,
            meter_max_count=int(skill_cfg.get("meter_max_count", 0)),
            identity=self.node_info.node_id,
            counterparty=peer_nid,
        )
        
        # Run admission pipeline
        result = run_admission(ctx)
        
        # Check admission result
        if result.gate.outcome == "hard_block":
            # Write admission decision receipt
            self._write_receipt(result.receipt, counterparty=peer_nid, sign=True)
            # v0.33.0: credit.sanctioned — hard limit block
            if self.bus:
                self.bus.emit("credit.sanctioned", counterparty=msg.public_key, limit_type="hard", identity=msg.public_key)
            self._emit_task_rejected(skill_name, msg.public_key, msg.task_id, "ADMISSION_BLOCKED")
            return self._sign(TaskResult(
                task_id=msg.task_id,
                status="failed",
                error={"code": "ADMISSION_BLOCKED",
                       "message": result.gate.reason}
            ))
        
        # Cache admission result for the worker (price, breakdown, prepaid decision)
        skill_price = result.pricing.final_price

        # Compute dedup hash (H18/H19)
        caller_node_id = hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest()
        canonical = json.dumps(msg.input_data, sort_keys=True, separators=(',', ':'))
        input_hash = hashlib.sha256(
            f"{msg.skill_name}:{canonical}:{caller_node_id}".encode()
        ).hexdigest()[:32]

        # Check for existing job (poll/dedup)
        existing = self.storage.get_async_job_by_hash(input_hash)
        if existing:
            return self._sign(TaskStatus(
                task_id=existing["job_id"],
                status=existing["status"],
                position=existing.get("position", 0)
            ))

        # H19: Generate job_id for async jobs, or use task_id for sync
        job_id = msg.task_id
        is_async = getattr(msg, "mode", "sync") == "async"
        if is_async:
            job_id = str(uuid.uuid4())

        # v0.37.0 A1: Local skill fast path — execute directly without queue
        # MUST still write receipts, emit bus events, check admission gate, respect max_concurrent
        if is_self_call and not is_async:
            active = self._skill_active.get(skill_name, 0)
            max_concurrent = self._get_skill_max_concurrent(skill_name)
            if active < max_concurrent:
                # Insert task record so telemetry (get_skill_task_stats, get_recent_tasks) works
                _fp_task = Task(
                    task_id=job_id,
                    skill_name=skill_name,
                    requester_node_id=msg.requester_node_id,
                    provider_node_id=self.node_info.node_id,
                    status="accepted",
                    input_data=msg.input_data,
                    created_at=time.time(),
                    updated_at=time.time(),
                    timeout_ms=msg.timeout_ms
                )
                await self._enqueue_write(self.storage.insert_task, _fp_task, self._public_key_hex)
                self._skill_active[skill_name] = active + 1
                try:
                    result_msg = await self._execute_local_fast_path(
                        msg, handler_fn, slow, skill_name, skill_price,
                        caller_node_id, job_id, input_hash
                    )
                    return result_msg
                finally:
                    self._skill_active[skill_name] = max(0, self._skill_active.get(skill_name, 0) - 1)
            # else: fall through to queue (concurrency cap hit)

        self._admission_cache[job_id] = {
            "price": result.pricing.final_price,
            "breakdown": dataclasses.asdict(self._pricing_result_to_breakdown(result.pricing)),
            "prepaid_action": result.prepaid.action if result.prepaid else "skip",
            "prepaid_amount": result.prepaid.amount if result.prepaid else 0.0,
        }

        task = Task(
            task_id=job_id,
            skill_name=skill_name,
            requester_node_id=msg.requester_node_id,
            provider_node_id=self.node_info.node_id,
            status="accepted",
            input_data=msg.input_data,
            created_at=time.time(),
            updated_at=time.time(),
            timeout_ms=msg.timeout_ms
        )
        await self._enqueue_write(self.storage.insert_task, task, self._public_key_hex)

        # Create a Future for the result
        loop = asyncio.get_running_loop()
        result_future = loop.create_future()

        if is_async:
            # Insert into async_jobs table
            expires_at = time.time() + 86400  # 24h grace period
            position = self._task_queue.qsize() + 1
            await self._enqueue_write(self.storage.insert_async_job, job_id, skill_name, caller_node_id, input_hash, position, expires_at)
            
            # Enqueue and return accepted immediately
            input_size = len(json.dumps(msg.input_data)) if msg.input_data else 0
            start_time = time.time()
            # Update msg with generated job_id for the worker
            msg_with_job = replace(msg, task_id=job_id)
            try:
                self._task_queue.put_nowait((msg_with_job, handler_fn, slow, input_size, start_time, result_future))
                # B3: task.queued event
                if self.bus:
                    self.bus.emit("task.queued",
                        skill_name=skill_name, caller_node=caller_node_id,
                        task_id=job_id, identity=caller_node_id,
                        queue_position=position)
                # B4: order_ack receipt — async task accepted into queue
                self._write_receipt(
                    document_type="order_ack",
                    payload={
                        "provider": self.node_info.node_id,
                        "caller": caller_node_id,
                        "skill_uri": f"knarr:///{skill_name}",
                        "queue": {"position": position, "estimated_wait_ms": None},
                    },
                    order_ref=job_id,
                    proof_purpose="assertion",
                    sign=False,
                )
                return self._sign(TaskStatus(task_id=job_id, status="accepted", position=position))
            except asyncio.QueueFull:
                self._admission_cache.pop(job_id, None)
                logger.debug(f"PROVIDER_BUSY: skill={skill_name} task={job_id[:8]} queue_full")
                # v0.33.0: node.slots_exhausted + task.rejected
                if self.bus:
                    self.bus.emit("node.slots_exhausted", slots_used=self._active_workers, slots_total=self._task_slots, identity=self.node_info.node_id)
                self._emit_task_rejected(skill_name, msg.public_key, job_id, "QUEUE_FULL")
                err = {"code": "PROVIDER_BUSY", "message": "Provider queue full, try another provider"}
                await self._enqueue_write(self.storage.update_task_status, job_id, "failed", None, err, input_size, 0)
                await self._enqueue_write(self.storage.update_async_job_status, job_id, "failed", None, err)
                # L-13: receipt for queue-full rejection
                self._write_receipt(
                    document_type="order_ack",
                    payload={"skill_name": skill_name, "status": "rejected", "reason": "QUEUE_FULL"},
                    counterparty=caller_node_id,
                    order_ref=job_id,
                    proof_purpose="assertion",
                    sign=True,
                )
                return self._sign(TaskResult(task_id=msg.task_id, status="failed", error=err))

        # v0.33.0 C-track: configurable default timeout
        _default_timeout_s = float(self._config.get("skills", {}).get("default_timeout", 30))
        max_timeout = self._config.get("node", {}).get("max_task_timeout", 3600)
        _req_timeout_s = msg.timeout_ms / 1000.0 if msg.timeout_ms else _default_timeout_s
        if max_timeout > 0:
            handler_timeout = min(_req_timeout_s, max_timeout)
        else:
            handler_timeout = _req_timeout_s

        input_size = len(json.dumps(msg.input_data)) if msg.input_data else 0
        start_time = time.time()

        # Admission control: check worker saturation before queue depth
        # 1. Workers saturated + fast task → RETRY_AFTER (don't enqueue, consumer retries)
        # 2. Queue full → PROVIDER_BUSY (try another provider)
        # 3. Otherwise → enqueue and execute/acknowledge

        if self._active_workers >= self._task_slots and not slow:
            # Fast task, all workers busy: tell consumer to retry later
            self._admission_cache.pop(job_id, None)
            queue_depth = self._task_queue.qsize()
            stats = self.storage.get_skill_task_stats(skill_name)
            avg_ms = stats.get("avg_wall_time_ms", 30000) or 30000
            retry_after_s = max(1, int(((queue_depth + 1) * avg_ms) / 1000))
            logger.debug(f"RETRY_AFTER: skill={skill_name} task={msg.task_id[:8]} wait={retry_after_s}s queue={queue_depth}")
            err = {
                "code": "RETRY_AFTER",
                "message": f"Provider busy, try again in {retry_after_s} seconds",
                "retry_after_seconds": retry_after_s
            }
            await self._enqueue_write(
                self.storage.update_task_status, msg.task_id, "failed",
                None, err, input_size, 0
            )
            return self._sign(TaskResult(task_id=msg.task_id, status="failed", error=err))

        try:
            self._task_queue.put_nowait((msg, handler_fn, slow, input_size, start_time, result_future))
        except asyncio.QueueFull:
            self._admission_cache.pop(job_id, None)
            logger.debug(f"PROVIDER_BUSY: skill={skill_name} task={msg.task_id[:8]} queue_full")
            # v0.33.0: node.slots_exhausted + task.rejected
            if self.bus:
                self.bus.emit("node.slots_exhausted", slots_used=self._active_workers, slots_total=self._task_slots, identity=self.node_info.node_id)
            self._emit_task_rejected(skill_name, msg.public_key, msg.task_id, "QUEUE_FULL")
            err = {"code": "PROVIDER_BUSY", "message": "Provider queue full, try another provider"}
            await self._enqueue_write(
                self.storage.update_task_status, msg.task_id, "failed",
                None, err, input_size, 0
            )
            # L-13: receipt for queue-full rejection
            self._write_receipt(
                document_type="order_ack",
                payload={"skill_name": skill_name, "status": "rejected", "reason": "QUEUE_FULL"},
                counterparty=msg.public_key,
                order_ref=msg.task_id,
                proof_purpose="assertion",
                sign=True,
            )
            return self._sign(TaskResult(task_id=msg.task_id, status="failed", error=err))

        # Workers saturated (slow task): return queued status with position
        if self._active_workers >= self._task_slots:
            position = self._task_queue.qsize()
            # B4: order_ack receipt — sync task queued
            self._write_receipt(
                document_type="order_ack",
                payload={
                    "provider": self.node_info.node_id,
                    "caller": caller_node_id,
                    "skill_uri": f"knarr:///{skill_name}",
                    "queue": {"position": position, "estimated_wait_ms": None},
                },
                order_ref=msg.task_id,
                proof_purpose="assertion",
                sign=False,
            )
            return self._sign(TaskStatus(task_id=msg.task_id, status="queued", position=position))

        # Worker available: wait for result (fast) or return accepted (slow)
        if not slow:
            try:
                result_msg = await asyncio.wait_for(result_future, timeout=handler_timeout)
                return result_msg
            except asyncio.TimeoutError:
                err = {"code": "TIMEOUT", "message": "Task timed out"}
                return self._sign(TaskResult(task_id=msg.task_id, status="failed", error=err))
        else:
            return self._sign(TaskStatus(task_id=msg.task_id, status="accepted"))

    async def start_mcp_bridge(self, command: List[str],
                                tool_filter: Optional[Dict[str, Any]] = None,
                                tool_timeout: float = 30.0):
        """Starts an MCP bridge and adds it to the node."""
        from .mcp_bridge import MCPBridge
        bridge = MCPBridge(command, self, tool_filter, tool_timeout)
        await bridge.start()
        self._mcp_bridges.append(bridge)
        return bridge

    def resolve_peer(self, node_id: str, host: str, port: int) -> tuple:
        """Resolve peer address, applying config overrides then address book."""
        override = self._peer_overrides.get(node_id)
        if not override:
            for prefix, addr in self._peer_overrides.items():
                if node_id.startswith(prefix):
                    override = addr
                    break
        if override:
            return override
        # Fallback: address book (persisted peer addresses)
        addr = self.storage.get_address(node_id)
        if addr and addr["last_ip"] and addr["last_ip"] != "0.0.0.0" and addr["last_port"]:
            return (addr["last_ip"], addr["last_port"])
        return (host, port)

    async def force_heartbeat(self, node_id: str) -> dict:
        """Send an immediate heartbeat to a specific peer, triggering mail push in both directions.

        1. Pushes our outbox to them (flush_outbox covers this)
        2. Our heartbeat triggers their implicit-HB path, making them push their outbox to us
        Returns status dict for API callers.
        """
        _debug = self._config.get("mail", {}).get("debug", False)
        # Resolve address: peer table first, then peer_override with dummy
        peer_info = next((p for p in self.storage.get_peers() if p.node_id == node_id), None)
        if peer_info:
            h, p = self.resolve_peer(peer_info.node_id, peer_info.host, peer_info.port)
        else:
            h, p = self.resolve_peer(node_id, "0.0.0.0", 0)
            if h == "0.0.0.0" and p == 0:
                logger.warning(f"FORCE_HB to={node_id[:16]} FAIL: not in peers, no override")
                return {"status": "error", "reason": "peer_not_found"}

        if _debug:
            logger.info(f"FORCE_HB to={node_id[:16]} addr={h}:{p}")

        msg = self._sign(Heartbeat(
            node_id=self.node_info.node_id,
            timestamp=time.time(),
            version=__version__,
        ))
        try:
            resp = await self._pool.send(node_id, h, p, msg)
        except Exception as e:
            logger.warning(f"FORCE_HB to={node_id[:16]} SEND_FAIL: {e}")
            return {"status": "error", "reason": str(e)}

        if isinstance(resp, Heartbeat) and verify_message(resp) and verify_node_id(resp):
            self._peer_last_activity[node_id] = time.monotonic()
            if _debug:
                logger.info(f"FORCE_HB to={node_id[:16]} OK version={resp.version}")
            # Push our mail to them
            await self._sync.push_to_peer(node_id, h, p)
            return {"status": "ok", "peer_version": resp.version}
        else:
            logger.warning(f"FORCE_HB to={node_id[:16]} BAD_RESP: {type(resp).__name__}")
            return {"status": "error", "reason": "invalid_response"}

    async def _send_to_peer(self, peer: NodeInfo, msg: Message):
        """Sends a pre-signed message to a peer via connection pool (with outbound hook)."""
        try:
            if not await self._plugins.on_outbound(msg, peer):
                return  # Plugin suppressed outbound message
            # Egress check on PluginMessage payloads (user/plugin-controlled content)
            if isinstance(msg, PluginMessage) and msg.payload:
                payload_str = json.dumps(msg.payload) if isinstance(msg.payload, dict) else str(msg.payload)
                if not self._egress.check(payload_str):
                    logger.critical(f"EGRESS_BLOCK_PROTOCOL type=PluginMessage to={peer.node_id[:16]}")
                    # v0.33.0: security.egress_blocked
                    if self.bus:
                        self.bus.emit("security.egress_blocked", msg_type="PluginMessage", target=peer.node_id, identity=self.node_info.node_id)
                    return
            h, p = self.resolve_peer(peer.node_id, peer.host, peer.port)
            await self._pool.send(peer.node_id, h, p, msg, timeout=CONNECTION_TIMEOUT)
        except Exception:
            pass

    async def _send_to_peer_raw(self, peer: NodeInfo, msg: Message):
        """Sends a message bypassing outbound hooks. Used by plugins (e.g. Warn/Blocked delivery)."""
        try:
            h, p = self.resolve_peer(peer.node_id, peer.host, peer.port)
            await self._pool.send(peer.node_id, h, p, msg, timeout=CONNECTION_TIMEOUT)
        except Exception:
            pass

    async def _send_fire_forget(self, peer: NodeInfo, msg: Message):
        """Fire-and-forget: open, write, close. No pool lock, no response wait.
        Auto-signs the message if unsigned (V19-001)."""
        try:
            if not msg.signature:
                msg = self._sign(msg)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(peer.host, peer.port), timeout=5.0
            )
            await send_message(writer, msg)
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    def register_meta_realm(self, realm: str, config):
        """Register a meta realm (core or plugin)."""
        self._meta_realms[realm] = config

    def _update_meta_cache(self, realm: str, key: str, data: dict):
        """Write a cache file for meta/cockpit serving. Atomic write."""
        import json, os
        from pathlib import Path

        # Validate realm and key (path confinement)
        if "/" in realm or "\\" in realm or ".." in realm:
            logger.warning(f"META_CACHE_REJECT realm={realm} reason=path_traversal")
            return
        if "/" in key or "\\" in key or ".." in key:
            logger.warning(f"META_CACHE_REJECT key={key} reason=path_traversal")
            return

        cache_dir = Path(self._config.get("_config_dir", ".")) / "cache" / realm
        cache_dir.mkdir(parents=True, exist_ok=True)

        envelope = {
            "realm": realm, "query": key,
            "node_id": self.node_info.node_id,
            "timestamp": time.time(),
            "ttl": self._meta_ttl.get(realm, 300),
            "data": data
        }
        tmp = cache_dir / f"{key}.json.tmp"
        final = cache_dir / f"{key}.json"
        try:
            tmp.write_text(json.dumps(envelope, separators=(',', ':')))
            os.replace(str(tmp), str(final))  # atomic write
        except Exception as e:
            logger.warning(f"META_CACHE_WRITE_FAIL realm={realm} key={key}: {e}")
            # Clean up partial tmp file
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def refresh_node_meta(self):
        """Refresh node/info meta cache with current skills count and uptime."""
        uptime = int(time.monotonic() - self._start_time) if self._start_time else 0
        self._update_meta_cache("node", "info", {
            "node_id": self.node_info.node_id,
            "version": __version__,
            "jurisdiction": self._node_jurisdiction_wire,
            "skills_count": len(self._own_skills),
            "uptime": uptime,
        })

    async def _process_message_callback(self, msg: Message, peer_ip: str = ""):
        """Callback for plugins to deliver fire-and-forget messages into the node's processing pipeline."""
        await self._process_message(msg, peer_ip=peer_ip)

    # Per-skill secret store
    # Per-skill secret store — C2: delegated to SecretsManager in dht/secrets.py

    def load_secrets(self, secrets_path: str = ""):
        """Load per-skill secrets from vault. Delegates to SecretsManager."""
        self._secrets_mgr.load(secrets_path)
        # Keep _secrets dict in sync for any direct access
        self._secrets = self._secrets_mgr._secrets

    def _inject_secrets(self, skill_name: str, input_data: dict) -> dict:
        """Inject per-skill secrets into input_data. Delegates to SecretsManager."""
        return self._secrets_mgr.inject(skill_name, input_data)

    def get_secrets_summary(self) -> Dict[str, Any]:
        """Returns per-skill secret status for cockpit (values masked)."""
        return self._secrets_mgr.get_summary()

    def set_secret(self, skill_name: str, key: str, value: str):
        """Set a secret value and persist to vault."""
        self._secrets_mgr.set_secret(skill_name, key, value)
        # Keep _secrets dict in sync
        self._secrets = self._secrets_mgr._secrets

    def delete_secret(self, skill_name: str, key: str):
        """Delete a secret value from vault."""
        self._secrets_mgr.delete_secret(skill_name, key)
        # Keep _secrets dict in sync
        self._secrets = self._secrets_mgr._secrets

    # System Mail Handlers (v0.17.0) — moved to dht/mail_handlers.py (v0.43.0 C1)
    # Handlers are registered via self._mail_handlers in __init__.

    async def _handle_task_result_mail(self, item: dict):
        await self._mail_handlers._handle_task_result_mail(item)

    async def _handle_task_failed_mail(self, item: dict):
        await self._mail_handlers._handle_task_failed_mail(item)

    async def _handle_asset_fetch_mail(self, item: dict):
        await self._mail_handlers._handle_asset_fetch_mail(item)

    async def _handle_asset_ready_mail(self, item: dict):
        await self._mail_handlers._handle_asset_ready_mail(item)

    def _fetch_sidecar_asset(self, host: str, port: int, asset_hash: str):
        self._mail_handlers._fetch_sidecar_asset(host, port, asset_hash)

    # Cockpit Data Accessors
    def get_node_info(self) -> Dict[str, Any]:
        """Returns node identity and uptime. Feeds cockpit header."""
        return {
            "node_id": self.node_info.node_id,
            "host": self.node_info.host,
            "port": self.node_info.port,
            "public_key": self._public_key_hex,
            "uptime_seconds": int(time.monotonic() - self._start_time) if self._start_time else 0,
        }

    def get_peer_summary(self) -> List[Dict[str, Any]]:
        """Returns peer list with health data. Feeds cockpit peer panel."""
        peers = self.storage.get_peers_full()
        return [
            {
                "node_id": p["node_id"],
                "host": p["host"],
                "port": p["port"],
                "last_seen": p["last_seen"],
                "missed_heartbeats": 0,  # Legacy field, kept for cockpit compat. Implicit heartbeat uses _peer_last_activity.
                "load": p.get("load", -1),
            }
            for p in peers
        ]

    def get_skill_summary(self) -> List[Dict[str, Any]]:
        """Returns own skills with traffic data. Feeds cockpit skill panel."""
        result = []
        for name, sheet in self._own_skills.items():
            stats = self.storage.get_skill_task_stats(name)
            result.append({
                "name": name,
                "visibility": self._skill_visibility.get(name, "public"),
                "price": sheet.price,
                "max_input_size": sheet.max_input_size,
                "tasks_completed": stats["total_completed"],
                "avg_wall_time_ms": stats["avg_wall_time_ms"],
                "avg_input_bytes": stats["avg_input_bytes"],
            })
        return result

    def get_economy_summary(self) -> Dict[str, Any]:
        """Aggregated economy view: per-peer positions + summary. Feeds cockpit economy panel."""
        entries = self.storage.get_all_ledger_entries()
        peers = []
        total_red, total_black = 0.0, 0.0
        green, amber, red = 0, 0, 0
        for e in entries:
            pk = e["peer_public_key"]
            balance = e["balance"]
            # Determine credit limit from policy (default — no skill context)
            ic, mb = self.policy.initial_credit, self.policy.min_balance
            group_name = ""
            if self._group_engine is not None:
                try:
                    peer_nid = hashlib.sha256(bytes.fromhex(pk)).hexdigest()
                except ValueError:
                    peer_nid = pk  # fallback if pk is not valid hex
                peer_groups = self._group_engine.get_groups(peer_nid)
                if peer_groups:
                    group_name = peer_groups[0]  # first matching group for display
                    # Credit from new config — best across all matching groups
                    credit_cfg = self._config.get("credit", {}).get("group_limits", {})
                    for g in peer_groups:
                        if g in credit_cfg:
                            gcfg = credit_cfg[g]
                            if isinstance(gcfg, dict):
                                ic = max(ic, float(gcfg.get("initial_credit", ic)))
                                mb = min(mb, float(gcfg.get("min_balance", mb)))
                            else:
                                ic = max(ic, float(gcfg))
            # Backward compat: old-format GroupPolicy
            if not group_name:
                for g in self._group_policies:
                    if pk in g.members:
                        ic, mb = g.initial_credit, g.min_balance
                        group_name = g.name
                        break
            credit_range = ic - mb  # total credit window
            utilization = 0.0
            if credit_range > 0:
                utilization = max(0.0, min(100.0, ((ic - balance) / credit_range) * 100.0))
            status = "green" if utilization < 50 else ("amber" if utilization < 80 else "red")
            if balance < 0:
                total_red += balance
            else:
                total_black += balance
            if status == "green":
                green += 1
            elif status == "amber":
                amber += 1
            else:
                red += 1
            peers.append({
                "node_id": pk[:16],
                "public_key": pk,
                "group": group_name,
                "balance": balance,
                "currency": "KNARR",
                "credit_limit": credit_range,
                "utilization_pct": round(utilization, 1),
                "status": status,
                "tasks_provided": e["tasks_provided"],
                "tasks_consumed": e["tasks_consumed"],
                "last_activity": e["last_updated"],
                # B5: Economy summary consumer fields
                "prepaid": e.get("prepaid", 0.0),
                "pub_tab": e.get("pub_tab", 0.0),
                "soft_limit": e.get("soft_limit", 0.0),
                "hard_limit": e.get("hard_limit", 0.0),
            })
        # Revenue/cost by skill (approximated from tasks table) [N-1: labeled est.]
        tasks = self.storage.get_recent_tasks(limit=1000)
        revenue_by_skill = {}
        cost_by_skill = {}
        for t in tasks:
            if t.get("status") != "completed":
                continue
            skill = t.get("skill_name", "")
            if not skill:
                continue
            if t.get("provider_node_id") == self.node_info.node_id:
                r = revenue_by_skill.setdefault(skill, {"count": 0, "total": 0.0})
                r["count"] += 1
                sheet = self._own_skills.get(skill.lower())
                r["total"] += sheet.price if sheet else 1.0
            if t.get("requester_node_id") == self.node_info.node_id:
                c = cost_by_skill.setdefault(skill, {"count": 0, "total": 0.0})
                c["count"] += 1
                c["total"] += 1.0

        return {
            "peers": peers,
            "summary": {
                "total_red": round(total_red, 1),
                "total_black": round(total_black, 1),
                "net_position": round(total_red + total_black, 1),
                "peers_green": green,
                "peers_amber": amber,
                "peers_red": red,
            },
            "revenue_by_skill": [{"skill_name": k, **v} for k, v in
                                 sorted(revenue_by_skill.items(), key=lambda x: -x[1]["total"])],
            "cost_by_skill": [{"skill_name": k, **v} for k, v in
                              sorted(cost_by_skill.items(), key=lambda x: -x[1]["total"])],
            "wallet": self._wallet,
            "token_balance": self._token_balance,
            "sol_balance": self._sol_balance,
            "token_mint": self._token_mint,
        }

    def get_task_feed(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns recent tasks. Feeds cockpit activity feed."""
        return self.storage.get_recent_tasks(limit)

    def get_demand_summary(self) -> List[Dict[str, Any]]:
        """Returns demand data. Feeds cockpit demand panel."""
        return self.storage.get_demand()

    def get_writer_queue_depth(self) -> int:
        """Returns current writer queue depth. Feeds cockpit health indicator."""
        return self._write_queue.qsize()

    def get_queue_status(self) -> Dict[str, Any]:
        """Returns task queue status. Feeds cockpit queue panel."""
        if self._task_slots > 0:
            load = min(10, int((self._active_workers / self._task_slots) * 10))
        else:
            load = 0
        return {
            "task_slots": self._task_slots,
            "active_workers": self._active_workers,
            "queue_depth": self._task_queue.qsize(),
            "queue_max": self._task_slots * 2,
            "load": load,
        }

    def get_skill_stats(self, skill_name: str) -> Dict[str, Any]:
        """Returns detailed stats for a skill. Feeds cockpit skill detail view."""
        return self.storage.get_skill_task_stats(skill_name.lower())

    def get_reputation_summary(self) -> List[Dict[str, Any]]:
        """Returns reputation data for all providers this consumer has interacted with.
        Feeds cockpit data layer and 'knarr info --reputation'."""
        reputations = self.storage.get_all_provider_reputations()
        ledger_entries = {e["peer_public_key"]: e for e in self.storage.get_all_ledger_entries()}

        result = []
        for rep in reputations:
            node_id = rep["provider_node_id"]
            # Try to find matching ledger entry via tasks table
            # (provider_public_key links tasks to ledger)
            conn = self.storage._get_conn()
            cursor = conn.execute(
                "SELECT DISTINCT provider_public_key FROM tasks WHERE provider_node_id = ? AND provider_public_key != ''",
                (node_id,)
            )
            pub_key_row = cursor.fetchone()
            pub_key = pub_key_row[0] if pub_key_row else ""

            ledger = ledger_entries.get(pub_key, {})
            avg_quality = self.storage.get_average_quality_rating(node_id)
            
            entry_dict = {
                "provider_node_id": node_id,
                "provider_public_key": pub_key,
                "balance": ledger.get("balance", 0.0),
                "tasks_provided": ledger.get("tasks_provided", 0),
                "tasks_consumed": ledger.get("tasks_consumed", 0),
                "success_rate": rep.get("success_rate"),
                "avg_wall_time_ms": rep.get("avg_wall_time_ms"),
                "total_tasks_30d": rep.get("total_tasks", 0),
                "last_interaction": rep.get("last_interaction"),
                "first_seen": ledger.get("first_seen", 0.0),
            }
            if avg_quality is not None:
                entry_dict["avg_quality_rating"] = round(avg_quality, 1)
            result.append(entry_dict)
        return result

    def get_diversification_info(self) -> Dict[str, Any]:
        """Counterparty diversification data. Low count = Sybil warning for self-monitoring."""
        entries = self.storage.get_all_ledger_entries()
        total_provided = sum(e.get("tasks_provided", 0) for e in entries)
        total_consumed = sum(e.get("tasks_consumed", 0) for e in entries)
        return {
            "unique_counterparties": self.storage.get_counterparty_count(),
            "total_tasks_provided": total_provided,
            "total_tasks_consumed": total_consumed,
        }

    # Cockpit API Accessors
    def get_status(self) -> dict:
        """Node status summary for cockpit /api/status."""
        uptime = int(time.monotonic() - self._start_time) if self._start_time else 0
        network_skills = set()
        for entry in self.storage.query_all_active_skills():
            network_skills.add(entry["skill_sheet"]["name"])
        # Determine bootstrap_node_id (first bootstrap peer)
        bootstrap_node_id = None
        bootstrap_list = getattr(self, "_config", {}).get("network", {}).get("bootstrap", [])
        if bootstrap_list:
            # The first bootstrap peer's node_id if connected
            peers = self.storage.get_peers()
            if peers:
                bootstrap_node_id = peers[0].node_id
        return {
            "node_id": self.node_info.node_id,
            "version": __version__,
            "uptime_seconds": uptime,
            "peer_count": len(self.storage.get_peers()),
            "skill_count": len(self._handlers),
            "network_skill_count": len(network_skills),
            "task_count": len(self.storage.get_recent_tasks(limit=1000)),
            "task_slots": {
                "used": self._active_workers,
                "total": self._task_slots,
            },
            "advertise_host": self.node_info.host,
            "port": self.node_info.port,
            "latest_network_version": getattr(self, "_notified_version", None),
            "version_gated": self._version_gated,
            "auto_upgrade": getattr(self, "_config", {}).get("node", {}).get("auto_upgrade", False),
            "upgrading": getattr(self, "_upgrading", False),
            "bootstrap_node_id": bootstrap_node_id,
            "wallet": getattr(self, "_wallet", ""),
            # v0.33.0 C-track: expose active limits
            "limits": {
                "minimum_price": float(self._config.get("skills", {}).get("minimum_price", 0.0)),
                "default_timeout": float(self._config.get("skills", {}).get("default_timeout", 30)),
                "max_queue_depth": self._task_queue.maxsize,
                "min_peers": int(self._config.get("network", {}).get("min_peers", MIN_PEER_FLOOR)),
                "default_soft_limit": float(self._config.get("economy", {}).get("default_soft_limit", self.policy.initial_credit)),
                "default_hard_limit": float(self._config.get("economy", {}).get("default_hard_limit", self.policy.min_balance)),
            },
        }

    def get_peers(self) -> list:
        """Peer list for cockpit /api/peers."""
        return self.storage.get_peers_full()

    def get_skills(self) -> dict:
        """Local + network skills for cockpit /api/skills."""
        local = []
        for name, (handler_fn, slow) in self._handlers.items():
            entry = {
                "name": name,
                "handler": self._handler_specs.get(name, ""),
                "visibility": self._skill_visibility.get(name, "public"),
            }
            sheet = self._own_skills.get(name)
            if sheet:
                if sheet.uri:
                    entry["uri"] = sheet.uri
                if sheet.jurisdiction:
                    entry["jurisdiction"] = sheet.jurisdiction
            local.append(entry)

        network = {}
        for entry in self.storage.query_all_active_skills():
            skill_name = entry["skill_sheet"]["name"]
            if skill_name not in network:
                net_entry = {
                    "name": skill_name,
                    "version": entry["skill_sheet"].get("version", ""),
                    "description": entry["skill_sheet"].get("description", ""),
                    "tags": entry["skill_sheet"].get("tags", []),
                    "providers": [],
                }
                if entry["skill_sheet"].get("uri"):
                    net_entry["uri"] = entry["skill_sheet"]["uri"]
                if entry["skill_sheet"].get("jurisdiction"):
                    net_entry["jurisdiction"] = entry["skill_sheet"]["jurisdiction"]
                if entry["skill_sheet"].get("price"):
                    net_entry["price"] = entry["skill_sheet"]["price"]
                network[skill_name] = net_entry
            provider_entry = {
                "node_id": entry["node_id"],
                "host": entry["host"],
                "port": entry["port"],
                "sidecar_port": entry.get("sidecar_port", 0),
            }
            # Enrich with skill-level metadata for strategy selection (V011-003)
            sheet = entry.get("skill_sheet", {})
            if sheet.get("price") is not None:
                provider_entry["price"] = sheet["price"]
            if sheet.get("jurisdiction"):
                provider_entry["jurisdiction"] = sheet["jurisdiction"]
            if entry.get("_load") is not None:
                provider_entry["load"] = entry["_load"]
            network[skill_name]["providers"].append(provider_entry)

        return {"local": local, "network": list(network.values())}

    def get_tasks(self) -> list:
        """Recent task history for cockpit /api/tasks."""
        return self.storage.get_recent_tasks(limit=100)

    def get_ledger(self) -> list:
        """Bilateral ledger for cockpit /api/ledger."""
        entries = self.storage.get_all_ledger_entries()
        for e in entries:
            e["currency"] = "KNARR"
        return entries

    def get_skill_schema(self, skill_name: str) -> Optional[dict]:
        """Return skill sheet + providers for a named skill (network or local)."""
        skill_name = skill_name.lower()
        providers = []
        sheet_data = None
        # Check network skills
        for entry in self.storage.query_all_active_skills():
            if entry["skill_sheet"]["name"].lower() == skill_name:
                if sheet_data is None:
                    sheet_data = entry["skill_sheet"]
                providers.append({
                    "node_id": entry["node_id"],
                    "host": entry["host"],
                    "port": entry["port"],
                    "sidecar_port": entry.get("sidecar_port", 0),
                    "load": entry.get("load", -1),
                })
        if sheet_data:
            result = {
                "name": sheet_data["name"],
                "version": sheet_data.get("version", ""),
                "description": sheet_data.get("description", ""),
                "tags": sheet_data.get("tags", []),
                "input_schema": sheet_data.get("input_schema", {}),
                "output_schema": sheet_data.get("output_schema", {}),
                "price": sheet_data.get("price", 1.0),
                "providers": providers,
            }
            if sheet_data.get("input_schema_full"):
                result["input_schema_full"] = sheet_data["input_schema_full"]
            if sheet_data.get("input_spec"):
                result["input_spec"] = sheet_data["input_spec"]
            if sheet_data.get("uri"):
                result["uri"] = sheet_data["uri"]
            if sheet_data.get("jurisdiction"):
                result["jurisdiction"] = sheet_data["jurisdiction"]
            return result
        # Fall back to local skill sheet
        local_sheet = self._own_skills.get(skill_name)
        if local_sheet:
            result = {
                "name": local_sheet.name,
                "version": local_sheet.version,
                "description": local_sheet.description,
                "tags": local_sheet.tags,
                "input_schema": local_sheet.input_schema,
                "output_schema": local_sheet.output_schema,
                "price": local_sheet.price,
                "providers": [],
                "local": True,
            }
            if local_sheet.input_schema_full:
                result["input_schema_full"] = local_sheet.input_schema_full
            if local_sheet.input_spec:
                result["input_spec"] = local_sheet.input_spec
            if local_sheet.uri:
                result["uri"] = local_sheet.uri
            if local_sheet.jurisdiction:
                result["jurisdiction"] = local_sheet.jurisdiction
            return result
        return None

    # Asset access methods
    def get_asset(self, hash: str) -> bytes:
        """Read an asset from local sidecar storage."""
        ctx = TaskContext(self._asset_dir)
        return ctx.get_asset(hash)

    def store_asset(self, data: bytes) -> str:
        """Store binary data in local sidecar storage. Returns hash.

        Enforces sidecar size limits and updates sidecar metadata if running.
        """
        # Enforce sidecar limits if sidecar is active
        if self._sidecar:
            if len(data) > self._sidecar._max_asset_size:
                raise ValueError(f"Asset exceeds max size ({len(data)} > {self._sidecar._max_asset_size})")
            if self._sidecar._total_size + len(data) > self._sidecar._max_total_size:
                raise ValueError("Asset storage capacity exceeded")
        ctx = TaskContext(self._asset_dir)
        content_hash = ctx.store_asset(data)
        # Sync sidecar metadata so listing/accounting stays accurate
        if self._sidecar and content_hash not in self._sidecar._metadata:
            from .sidecar import AssetMetadata
            self._sidecar._metadata[content_hash] = AssetMetadata(
                size=len(data), uploaded_at=time.time(), uploader_key="")
            self._sidecar._total_size += len(data)
        return content_hash

    def asset_path(self, hash: str) -> str:
        """Return local file path for an asset hash."""
        ctx = TaskContext(self._asset_dir)
        return ctx.asset_path(hash)

    def _filter_providers_by_group(self, providers: list) -> list:
        """Apply require/prefer/exclude group filters to provider list."""
        if self._group_engine is None:
            return providers

        disc_cfg = self._config.get("discovery", {})
        require = set(disc_cfg.get("require_groups", []))
        prefer = list(disc_cfg.get("prefer_groups", []))
        exclude = set(disc_cfg.get("exclude_groups", []))

        if not require and not prefer and not exclude:
            return providers

        def _get_nid(p):
            if isinstance(p, dict):
                return p.get("node_id", "") or p.get("provider_node_id", "")
            return getattr(p, "node_id", "") or getattr(p, "provider_node_id", "")

        filtered = []
        for p in providers:
            nid = _get_nid(p)
            if not nid:
                filtered.append(p)
                continue

            # Exclude check
            if exclude and any(self._group_engine.is_member(nid, g) for g in exclude):
                continue

            # Require check
            if require and not any(self._group_engine.is_member(nid, g) for g in require):
                continue

            filtered.append(p)

        # Prefer sort: providers in preferred groups first
        if prefer:
            def prefer_key(p):
                nid = _get_nid(p)
                for i, g in enumerate(prefer):
                    if self._group_engine.is_member(nid, g):
                        return i
                return len(prefer)
            filtered.sort(key=prefer_key)

        return filtered

    def _get_initial_trust(self, node_id: str) -> float:
        """Determine initial trust from group membership."""
        trust_cfg = self._config.get("reputation", {}).get("initial_trust", {})
        default_trust = float(trust_cfg.get("default", 0.3))
        if not trust_cfg or self._group_engine is None:
            return default_trust

        groups = self._group_engine.get_groups(node_id)
        best_trust = default_trust
        for g in groups:
            if g in trust_cfg:
                best_trust = max(best_trust, float(trust_cfg[g]))
        return best_trust

    def _resolve_price(self, node_id: str, base_price: float, skill_name: str = "") -> tuple:
        """Resolve price through the configured pricing engine."""
        import math
        if not math.isfinite(base_price) or base_price < 0:
            logger.warning("PRICING_INVALID base_price=%s skill=%s", base_price, skill_name)
            from ..core.pricing import PriceBreakdown
            return 0.0, PriceBreakdown(base_price=0.0, cost_projection=None, rules_applied=[], discount_mode="", floor_price=0.0, floor_applied=True, promotion_applied=False)
        engine = str(self._config.get("pricing", {}).get("engine", "builtin") or "builtin").strip().lower()
        if engine == "builtin":
            return self._resolve_price_builtin(node_id, base_price, skill_name)
        if engine == "module":
            from ..commerce.pricing_engine import PricingRequest, resolve_price

            pricing_result = resolve_price(
                PricingRequest(
                    base_price=base_price,
                    skill_name=skill_name,
                    peer_node_id=node_id,
                    peer_groups=set(self._group_engine.get_groups(node_id)) if self._group_engine else set(),
                    discount_rules=self._load_discount_rules(node_id, skill_name),
                    cost_projection=self._get_cost_projection(skill_name),
                    skill_min_price=self._get_skill_min_price(skill_name),
                ),
                self._build_pricing_config(skill_name),
            )
            return pricing_result.final_price, self._pricing_result_to_breakdown(pricing_result)

        logger.warning("PRICING_ENGINE_UNKNOWN engine=%s falling back to builtin", engine)
        return self._resolve_price_builtin(node_id, base_price, skill_name)

    def _resolve_price_builtin(self, node_id: str, base_price: float, skill_name: str = "") -> tuple:
        """Compute final price with structured breakdown.

        Returns (final_price: float, breakdown: PriceBreakdown).
        """
        from ..core.pricing import PriceBreakdown

        # B2/FINDING-G: Free skills stay free — bypass all discount/floor/surcharge logic
        if base_price == 0.0:
            return (0.0, PriceBreakdown(
                base_price=0.0,
                cost_projection=None,
                rules_applied=[],
                discount_mode="",
                floor_price=0.0,
                floor_applied=False,
                promotion_applied=False,
                final_price=0.0,
            ))

        # 1. Load applicable discounts from SQL
        rules_applied = []
        groups = set()
        if self._group_engine:
            groups = set(self._group_engine.get_groups(node_id))

        discount_rows = []
        if groups:
            try:
                conn = self.storage._get_conn()
                placeholders = ",".join("?" * len(groups))
                rows = conn.execute(f"""
                    SELECT name, group_name, skill_group, effect_pct, priority
                    FROM pricing_discounts
                    WHERE group_name IN ({placeholders})
                      AND (skill_group = '*' OR skill_group = ?)
                      AND active = 1
                    ORDER BY priority DESC
                """, list(groups) + [skill_name]).fetchall()
                discount_rows = [
                    {"name": r[0], "group_name": r[1], "skill_group": r[2], "effect_pct": r[3], "priority": r[4]}
                    for r in rows
                ]
            except Exception as e:
                logger.warning(f"PRICING_SQL_FAIL: {e}")
                discount_rows = []

        # Fallback: TOML discounts (dual-read for one version)
        if not discount_rows and groups:
            toml_discounts = self._config.get("pricing", {}).get("discounts", {})
            for group_name, pct_off in toml_discounts.items():
                if group_name in groups:
                    discount_rows.append({
                        "name": f"toml_{group_name}", "group_name": group_name,
                        "skill_group": "*", "effect_pct": float(pct_off), "priority": 0
                    })

        # 2. Apply stacking mode
        discount_mode = self._config.get("pricing", {}).get("discount_mode", "multiplicative")
        price = base_price

        if discount_rows:
            if discount_mode == "multiplicative":
                for rule in discount_rows:
                    factor = 1.0 - (rule["effect_pct"] / 100.0)
                    price *= factor
                    rules_applied.append({"name": rule["name"], "effect_pct": rule["effect_pct"], "factor": factor})
            elif discount_mode == "additive":
                total_pct = sum(r["effect_pct"] for r in discount_rows)
                price = base_price * (1.0 - total_pct / 100.0)
                rules_applied = [{"name": r["name"], "effect_pct": r["effect_pct"]} for r in discount_rows]
            elif discount_mode == "best_wins":
                best = max(discount_rows, key=lambda r: r["effect_pct"])
                price = base_price * (1.0 - best["effect_pct"] / 100.0)
                rules_applied = [{"name": best["name"], "effect_pct": best["effect_pct"]}]

        # 3. Apply discount cap
        caps = self._config.get("pricing", {}).get("caps", {})
        max_pct = float(caps.get(skill_name, caps.get("*", 100.0)))
        max_discount = base_price * (max_pct / 100.0)
        if base_price - price > max_discount:
            price = base_price - max_discount

        # 4. Compute floor
        cost_projection = None
        try:
            conn = self.storage._get_conn()
            row = conn.execute(
                "SELECT total_cost FROM skill_cost_projection WHERE skill_name = ?",
                (skill_name,)
            ).fetchone()
            if row:
                cost_projection = row[0]
        except Exception:
            pass

        markup_min = float(self._config.get("pricing", {}).get("floors", {}).get("markup_minimum", 1.1))
        static_floor = float(self._config.get("pricing", {}).get("min_price", 0.01))
        skill_floor = float(self._config.get("skills", {}).get(skill_name, {}).get("min_price", static_floor) if isinstance(self._config.get("skills", {}).get(skill_name), dict) else static_floor)

        if cost_projection is not None and cost_projection > 0:
            dynamic_floor = cost_projection * markup_min
            floor_price = max(dynamic_floor, skill_floor)
        else:
            floor_price = skill_floor

        # 5. Clamp to floor
        floor_applied = price < floor_price
        if floor_applied:
            price = floor_price

        # v0.33.0 C-track: global minimum price floor from config
        global_min = float(self._config.get("skills", {}).get("minimum_price", 0.0))
        if price < global_min:
            price = global_min
            floor_applied = True

        # 6. Build breakdown
        breakdown = PriceBreakdown(
            base_price=base_price,
            cost_projection=cost_projection,
            rules_applied=rules_applied,
            discount_mode=discount_mode,
            floor_price=max(floor_price, global_min),
            floor_applied=floor_applied,
            promotion_applied=False,  # interface only, no implementation yet
            final_price=round(price, 6),
        )

        return (round(price, 6), breakdown)

    def _get_skill_runtime_config(self, skill_name: str) -> Dict[str, Any]:
        """Return runtime skill config from [skills."name"] in knarr.toml."""
        skills_cfg = self._config.get("skills", {})
        skill_cfg = skills_cfg.get(skill_name, {})
        return dict(skill_cfg) if isinstance(skill_cfg, dict) else {}

    def _get_skill_min_price(self, skill_name: str) -> Optional[float]:
        skill_cfg = self._get_skill_runtime_config(skill_name)
        if "min_price" not in skill_cfg:
            return None
        try:
            return float(skill_cfg["min_price"])
        except (TypeError, ValueError):
            return None

    def _get_cost_projection(self, skill_name: str) -> Optional[float]:
        try:
            row = self.storage._get_conn().execute(
                "SELECT total_cost FROM skill_cost_projection WHERE skill_name = ?",
                (skill_name,),
            ).fetchone()
            return float(row[0]) if row and row[0] is not None else None
        except Exception:
            return None

    def _load_discount_rules(self, node_id: str, skill_name: str):
        from ..commerce.pricing_engine import DiscountRule

        groups = set(self._group_engine.get_groups(node_id)) if self._group_engine else set()
        rules = []
        if groups:
            try:
                conn = self.storage._get_conn()
                placeholders = ",".join("?" * len(groups))
                rows = conn.execute(
                    f"""
                    SELECT name, group_name, skill_group, effect_pct, priority
                    FROM pricing_discounts
                    WHERE group_name IN ({placeholders})
                      AND (skill_group = '*' OR skill_group = ?)
                      AND active = 1
                    ORDER BY priority DESC
                    """,
                    list(groups) + [skill_name],
                ).fetchall()
                rules = [
                    DiscountRule(
                        name=r[0], group_name=r[1], skill_group=r[2],
                        effect_pct=float(r[3]), priority=int(r[4]),
                    )
                    for r in rows
                ]
            except Exception as e:
                logger.warning(f"PRICING_SQL_FAIL: {e}")
                rules = []

        if not rules and groups:
            for group_name, pct_off in self._config.get("pricing", {}).get("discounts", {}).items():
                if group_name in groups:
                    rules.append(
                        DiscountRule(
                            name=f"toml_{group_name}", group_name=group_name,
                            skill_group="*", effect_pct=float(pct_off), priority=0,
                        )
                    )
        return rules

    @staticmethod
    def _pricing_result_to_breakdown(pricing_result) -> PriceBreakdown:
        return PriceBreakdown(
            base_price=pricing_result.base_price,
            cost_projection=pricing_result.cost_projection,
            rules_applied=pricing_result.rules_applied,
            discount_mode=pricing_result.discount_mode,
            floor_price=pricing_result.floor_price,
            floor_applied=pricing_result.floor_applied,
            promotion_applied=False,
            final_price=pricing_result.final_price,
        )

    def _build_pricing_config(self, skill_name: str = ""):
        """Build PricingConfig from node config."""
        from ..commerce.pricing_engine import PricingConfig

        pricing_cfg = self._config.get("pricing", {})
        caps = pricing_cfg.get("caps", {})
        floors = pricing_cfg.get("floors", {})
        return PricingConfig(
            discount_mode=str(pricing_cfg.get("discount_mode", "multiplicative")),
            discount_cap_pct=float(caps.get(skill_name, caps.get("*", 100.0))),
            markup_minimum=float(floors.get("markup_minimum", 1.1)),
            min_price=float(pricing_cfg.get("min_price", 0.01)),
            global_min_price=float(self._config.get("skills", {}).get("minimum_price", 0.0)),
            decay=float(pricing_cfg.get("decay", 1.0)),
        )

    async def _apply_provider_billing(
        self,
        msg: TaskRequest,
        job_id_for_update: str,
        skill_name: str,
        skill_price: float,
        skill_cfg: Optional[Dict[str, Any]] = None,
        prepaid_action: str = "skip",
        prepaid_amount: float = 0.0,
    ):
        """Apply provider-side billing: prepaid, held, or direct."""
        skill_cfg = skill_cfg or {}
        if prepaid_action == "deduct":
            await self._enqueue_write(
                self.storage.deduct_prepaid,
                msg.public_key,
                prepaid_amount or max(skill_price, 0.0),
            )
            return
        if skill_cfg.get("hold"):
            hold_amount = abs(skill_price)
            await self._enqueue_write(self.storage.hold_balance, msg.public_key, hold_amount)
            if self.bus:
                self.bus.emit(
                    "skill.held",
                    task_id=job_id_for_update,
                    skill=skill_name,
                    peer=msg.public_key,
                    amount=hold_amount,
                    price=skill_price,
                )
            logger.info(f"SKILL_HELD task={job_id_for_update[:8]} amount={hold_amount}")
            return

        old_balance = self.storage.get_ledger_balance(msg.public_key)
        await self._enqueue_write(self.storage.update_ledger_provider, msg.public_key, skill_price)
        new_balance = self.storage.get_ledger_balance(msg.public_key)
        if self.bus:
            self.bus.emit(
                "credit.change",
                direction="provider",
                counterparty=getattr(msg, "public_key", ""),
                amount=skill_price,
                reference=job_id_for_update,
                identity=getattr(msg, "public_key", ""),
            )
        if old_balance is not None and new_balance is not None:
            self._check_credit_restored(msg.public_key, old_balance, new_balance)

    async def _handle_settlement_soft_threshold(self, item: dict):
        """Evaluate a queued netting trigger and send a settlement request if needed."""
        from ..commerce.settlement_engine import SettlementInput, evaluate_settlement
        from ..commerce.settlement_execution import prepare_settlement

        body = item.get("body") or {}
        peer_key = self._resolve_settlement_peer_key(
            item, key_names=("peer_public_key", "counterparty_key", "peer_key", "proposer_key")
        )
        if not peer_key:
            logger.warning(f"SETTLEMENT_SOFT_THRESHOLD_MISSING_PEER id={item.get('id')}")
            return

        entry = self.storage.get_or_create_ledger_entry(peer_key)
        soft_limit, hard_limit = self._resolve_policy(peer_key, "")
        current_balance = float(body.get("current_balance", getattr(entry, "balance", 0.0) or 0.0))
        credit_limit = abs(float(soft_limit) - float(hard_limit))
        utilization_pct = body.get("utilization_pct")
        if utilization_pct is None:
            utilization = abs(current_balance) / abs(hard_limit) if hard_limit else 0.0
        else:
            utilization = float(utilization_pct) / 100.0

        decision = evaluate_settlement(
            SettlementInput(
                peer_key=peer_key,
                balance=current_balance,
                prepaid=float(body.get("prepaid", 0.0)),
                pub_tab=float(body.get("pub_tab", 0.0)),
                soft_limit=float(soft_limit),
                hard_limit=float(hard_limit),
                credit_limit=float(credit_limit),
                tasks_provided=int(body.get("tasks_provided", getattr(entry, "tasks_provided", 0))),
                tasks_consumed=int(body.get("tasks_consumed", getattr(entry, "tasks_consumed", 0))),
                utilization=float(utilization),
            ),
            self._get_settlement_config(),
        )
        if decision.action != "settle":
            return

        prepared_doc = await prepare_settlement(
            node_id=self.node_info.node_id,
            peer_key=peer_key,
            amount=decision.amount,
            formula=(
                f"balance={current_balance:.6f} hard_limit={float(hard_limit):.6f} "
                f"target_utilization={decision.target_utilization:.6f}"
            ),
            proposer_balance=current_balance,
            counterparty_balance_claimed=float(body.get("counterparty_balance_claimed", -current_balance)),
            utilization=float(utilization),
            target_utilization=decision.target_utilization,
            signing_key=self._signing_key,
            storage=self.storage,
            bus=self.bus,
        )

        await self._sync.enqueue(
            to_node=self._resolve_settlement_target_node(peer_key),
            msg_type="knarr/commerce/settle_request",
            body={
                "type": "knarr/commerce/settle_request",
                "document": prepared_doc,
                "peer_key": getattr(self, "_public_key_hex", "") or self.node_info.node_id,
                "counterparty_key": peer_key,
                "amount": abs(float(decision.amount)),
                "current_balance": current_balance,
                "credit_limit": float(credit_limit),
                "provider_wallet": self._get_settlement_wallet(),
                "requested_action": "settle_to_zero",
                "target_utilization": decision.target_utilization,
                "timestamp": time.time(),
            },
            system=True,
            ttl_hours=24,
        )

    async def _handle_settlement_request(self, item: dict):
        """Countersign and execute an inbound settlement request."""
        from ..commerce.settlement_execution import execute_settlement
        from ..core.proof import sign_document

        body = item.get("body") or {}
        prepared_doc = body.get("document")
        if not isinstance(prepared_doc, dict):
            logger.warning(f"SETTLEMENT_REQUEST_INVALID id={item.get('id')} missing document")
            return
        if self._signing_key is None:
            raise RuntimeError("Settlement request processing requires a signing key")

        # A2: countersign with #key-1 (not #cockpit-1)
        payload = {key: value for key, value in prepared_doc.items() if key != "proof"}
        countersigned_doc = sign_document(
            payload,
            self._signing_key,
            f"did:knarr:{self.node_info.node_id}#key-1",
        )
        peer_key = self._resolve_settlement_peer_key(
            item, key_names=("peer_key", "proposer_key", "counterparty_key", "peer_public_key")
        )

        # A1: extract proposer verify key from prepared_doc proof verificationMethod.
        # Resolution strategy:
        #   1. Try resolve_did_fragment (works when node_id = pubkey hex)
        #   2. Fall back to peer_key from the mail body (= proposer's _public_key_hex)
        #   3. If both fail, log error and return — do NOT fall back to local key
        proposer_verify_key = None
        proof_vm = (prepared_doc.get("proof") or {}).get("verificationMethod", "")
        if proof_vm:
            resolved = self.resolve_did_fragment(proof_vm)
            if resolved is not None:
                proposer_verify_key = resolved
                logger.debug(f"SETTLEMENT_PROPOSER_KEY resolved via DID from {proof_vm!r}")
            elif peer_key:
                # peer_key = proposer's raw public key hex (sent alongside document)
                try:
                    proposer_verify_key = VerifyKey(bytes.fromhex(peer_key))
                    logger.debug(f"SETTLEMENT_PROPOSER_KEY resolved via peer_key={peer_key[:16]!r}")
                except Exception as exc:
                    logger.error(
                        "SETTLEMENT_PROPOSER_KEY_FAIL id=%s peer_key=%s error=%s — cannot verify, dropping",
                        item.get("id"), peer_key[:16] if peer_key else "", exc,
                    )
                    return
            else:
                logger.error(
                    "SETTLEMENT_PROPOSER_KEY_UNRESOLVED id=%s vm=%s — no peer_key available, dropping",
                    item.get("id"), proof_vm,
                )
                return
        else:
            logger.error(
                "SETTLEMENT_REQUEST_INVALID id=%s reason=missing_verification_method",
                item.get("id"),
            )
            return

        async def _send_confirmation_mail(to_node: str, msg_type: str, body: dict, system: bool = True):
            await self._sync.enqueue(
                to_node=item.get("from_node") or to_node,
                msg_type="knarr/commerce/settlement_confirmation",
                body=self._build_settlement_confirmation_body(
                    peer_key=peer_key,
                    amount=body.get("amount", 0.0),
                    accepted_receipt_id=body.get("accepted_receipt_id", ""),
                    settle_request_ref=prepared_doc.get("receipt_id", ""),
                ),
                system=system,
                ttl_hours=24,
            )

        receipt_id = await execute_settlement(
            prepared_doc=prepared_doc,
            countersigned_doc=countersigned_doc,
            node_verify_key=proposer_verify_key,
            authority_verify_key=self._signing_key.verify_key,
            node_id=self.node_info.node_id,
            signing_key=self._signing_key,
            peer_key=peer_key,
            storage=self.storage,
            send_mail_fn=_send_confirmation_mail,
            bus=self.bus,
            config=self._config,
        )
        if self.bus:
            self.bus.emit(
                "settlement.executed",
                receipt_id=receipt_id,
                peer=peer_key,
                amount=abs(float(body.get("amount", 0.0))),
                identity=self.node_info.node_id,
            )

    async def _handle_settlement_confirmation(self, item: dict):
        """Finalize a confirmed settlement and zero the bilateral ledger position."""
        from ..commerce.settlement_execution import write_settlement_processed

        body = item.get("body") or {}
        peer_key = self._resolve_settlement_peer_key(
            item, key_names=("peer_key", "proposer_key", "counterparty_key", "peer_public_key")
        )
        if not peer_key:
            logger.warning(f"SETTLEMENT_CONFIRM_INVALID id={item.get('id')} missing peer")
            return
        if self._signing_key is None:
            raise RuntimeError("Settlement confirmation processing requires a signing key")

        prior_balance = float(self.storage.get_ledger_balance(peer_key) or 0.0)
        if prior_balance > 0:
            await self._enqueue_write(self.storage.update_ledger_provider, peer_key, prior_balance)
        elif prior_balance < 0:
            await self._enqueue_write(self.storage.update_ledger_consumer, peer_key, abs(prior_balance))
        final_balance = float(self.storage.get_ledger_balance(peer_key) or 0.0)

        receipt_id = await write_settlement_processed(
            node_id=self.node_info.node_id,
            peer_key=peer_key,
            amount_settled=abs(float(body.get("amount_settled", 0.0))),
            ledger_delta=-prior_balance,
            final_balance=final_balance,
            accepted_receipt_id=str(body.get("accepted_receipt_id", "")),
            settle_request_ref=str(body.get("settle_request_ref", "")),
            signing_key=self._signing_key,
            storage=self.storage,
            bus=self.bus,
        )
        if self.bus:
            self.bus.emit(
                "settlement.confirmed",
                receipt_id=receipt_id,
                peer=peer_key,
                amount=abs(float(body.get("amount_settled", 0.0))),
                identity=self.node_info.node_id,
            )

    def _get_settlement_wallet(self) -> str:
        """Return a wallet-like identifier compatible with settlement mail schema."""
        wallet = getattr(self, "_wallet", "") or ""
        if 32 <= len(wallet) <= 44:
            return wallet
        node_id = getattr(getattr(self, "node_info", None), "node_id", "") or ""
        if len(node_id) >= 32:
            return node_id[:44]
        public_key = getattr(self, "_public_key_hex", "") or ""
        if len(public_key) >= 32:
            return public_key[:44]
        return "0" * 32

    def _resolve_settlement_target_node(self, peer_key: str) -> str:
        """Resolve a peer public key to a node id for outbound settlement mail."""
        node_id = None
        if hasattr(self.storage, "get_node_id_for_public_key"):
            node_id = self.storage.get_node_id_for_public_key(peer_key)
        if node_id:
            return node_id
        try:
            return hashlib.sha256(bytes.fromhex(peer_key)).hexdigest()
        except Exception:
            return peer_key

    def resolve_did_fragment(self, did_string: str) -> Optional[VerifyKey]:
        """Resolve did:knarr:<node_id>#<fragment> to an Ed25519 verify key."""
        if not isinstance(did_string, str) or not did_string.startswith("did:knarr:") or "#" not in did_string:
            return None
        node_part, fragment = did_string[len("did:knarr:"):].split("#", 1)
        node_id = node_part.strip()
        fragment = fragment.strip()
        if not node_id or not fragment:
            return None

        own_ids = {self.node_info.node_id, getattr(self, "_public_key_hex", "")}
        if node_id in own_ids:
            if fragment == "key-1":
                return self._signing_key.verify_key if self._signing_key else None
            if fragment == "cockpit-1":
                cockpit_key = getattr(self, "_cockpit_signing_key", None)
                if cockpit_key is not None:
                    return cockpit_key.verify_key
                return self._signing_key.verify_key if self._signing_key else None
            if fragment == "thrall-1":
                thrall_key = getattr(self, "_thrall_signing_key", None)
                return thrall_key.verify_key if thrall_key is not None else None
            return None

        return None

    def _resolve_settlement_peer_key(
        self,
        item: dict,
        key_names: Optional[tuple] = None,
    ) -> str:
        """Resolve the best available peer key from a queue item body."""
        body = item.get("body") if isinstance(item, dict) else {}
        if not isinstance(body, dict):
            body = {}
        for key in key_names or ("peer_public_key", "peer_key", "proposer_key", "counterparty_key"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
        fallback = item.get("from_node") if isinstance(item, dict) else ""
        return fallback if isinstance(fallback, str) else ""

    def _build_settlement_confirmation_body(
        self,
        peer_key: str,
        amount: float,
        accepted_receipt_id: str,
        settle_request_ref: str,
    ) -> Dict[str, Any]:
        """Build a settlement_confirmation message body."""
        seed = hashlib.sha256(
            f"{accepted_receipt_id}:{settle_request_ref}:{peer_key}".encode("utf-8")
        ).hexdigest()
        return {
            "type": "knarr/commerce/settlement_confirmation",
            "tx_hash": (seed + ("x" * 88))[:88],
            "amount_settled": abs(float(amount)),
            "timestamp": time.time(),
            "peer_key": peer_key,
            "accepted_receipt_id": accepted_receipt_id,
            "settle_request_ref": settle_request_ref,
        }

    def _get_settlement_config(self) -> dict:
        """Resolve settlement config by merging [economy.settlement] (base) and [settlement] (override)."""
        base = self._config.get("economy", {}).get("settlement", {})
        override = self._config.get("settlement", {})
        merged = dict(base)
        merged.update(override)
        return merged

    def _resolve_policy(self, public_key: str, skill_name: str) -> tuple:
        """Returns (initial_credit, min_balance) for this peer+skill combination.

        Evaluation order: skill override -> group credit limits -> old-format group -> default.
        """
        # 1. Base from default policy, overridden by economy config
        _econ = self._config.get("economy", {})
        base_ic = float(_econ.get("default_soft_limit", self.policy.initial_credit))
        base_mb = float(_econ.get("default_hard_limit", self.policy.min_balance))

        # 2. Skill override (partial — may set one or both)
        skill_pol = self._skill_policies.get(skill_name)
        if skill_pol:
            if skill_pol.initial_credit is not None:
                base_ic = skill_pol.initial_credit
            if skill_pol.min_balance is not None:
                base_mb = skill_pol.min_balance

        # Convert public_key → node_id for group membership lookups
        node_id = hashlib.sha256(bytes.fromhex(public_key)).hexdigest()

        # 3. Group credit limits — best across all matching groups
        best_ic, best_mb = base_ic, base_mb
        matched_new = False

        if self._group_engine is not None:
            groups = self._group_engine.get_groups(node_id)
            credit_cfg = self._config.get("credit", {}).get("group_limits", {})
            for g in groups:
                if g in credit_cfg:
                    gcfg = credit_cfg[g]
                    if isinstance(gcfg, dict):
                        gic = float(gcfg.get("initial_credit", base_ic))
                        gmb = float(gcfg.get("min_balance", base_mb))
                    else:
                        # Simple format: group_name = value (initial_credit only)
                        gic = float(gcfg)
                        gmb = base_mb
                    best_ic = max(best_ic, gic)
                    best_mb = min(best_mb, gmb)
                    matched_new = True

        # 4. Backward compat: old-format GroupPolicy objects carry credit on the group
        if not matched_new:
            for gp in self._group_policies:
                if node_id in gp.members:
                    best_ic = max(best_ic, gp.initial_credit)
                    best_mb = min(best_mb, gp.min_balance)
                    break

        return best_ic, best_mb

    # Background task loops
    async def _heartbeat_loop(self):
        while self._running:
            await asyncio.sleep(HEARTBEAT_CHECK_INTERVAL)
            try:
                await self._heartbeat_tick()
            except Exception:
                logger.error("Heartbeat loop error", exc_info=True)

    # v0.41.0 A2: Independent background task loops for network I/O
    async def _flush_outbox_loop(self):
        """Independent background loop for flushing mail outbox."""
        interval = max(1.0, float(self._config.get("node", {}).get("flush_interval", 10)))
        while self._running:
            await asyncio.sleep(interval)
            try:
                await asyncio.wait_for(self._sync.flush_outbox(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("FLUSH_TIMEOUT flush_outbox exceeded 5s deadline")
            except Exception as e:
                logger.warning(f"FLUSH_OUTBOX_FAIL: {e}")

    async def _pull_from_correspondents_loop(self):
        """Independent background loop for pulling mail from correspondents."""
        interval = max(1.0, float(self._config.get("mail", {}).get("pull_interval", 300)))
        while self._running:
            await asyncio.sleep(interval)
            try:
                await asyncio.wait_for(self._sync.pull_from_correspondents(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("PULL_TIMEOUT pull_from_correspondents exceeded 5s deadline")
            except Exception as e:
                logger.warning(f"PULL_FROM_CORRESPONDENTS_FAIL: {e}")

    async def _peer_heartbeat_sweep_loop(self):
        """Independent background loop for peer heartbeat sweep."""
        interval = max(1.0, float(self._config.get("node", {}).get("sweep_interval", 10)))
        while self._running:
            await asyncio.sleep(interval)
            try:
                peers = self.storage.get_peers()
                if not peers:
                    if self._bootstrap_peers:
                        logger.warning("No peers — attempting re-bootstrap")
                        if self.bus:
                            self.bus.emit("node.rebootstrap", reason="no_peers", identity=self.node_info.node_id)
                        try:
                            await self.join(self._bootstrap_peers)
                        except Exception as e:
                            logger.warning(f"Re-bootstrap failed: {e}")
                            if self.bus:
                                self.bus.emit("node.rebootstrap_failed", error=str(e), identity=self.node_info.node_id)
                    continue

                now = time.monotonic()
                await asyncio.wait_for(
                    self._peer_heartbeat_sweep(peers, now),
                    timeout=PEER_HEARTBEAT_SWEEP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "PEER_SWEEP_TIMEOUT peer heartbeat sweep exceeded %.1fs",
                    PEER_HEARTBEAT_SWEEP_TIMEOUT,
                )
            except Exception as e:
                logger.warning(f"PEER_HEARTBEAT_SWEEP_FAIL: {e}")

    async def _settlement_consumer_loop(self):
        """Process pending settlement queue items on a tick interval."""
        interval = max(0.1, float(self._get_settlement_config().get("consumer_interval", 60)))
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self._settlement_consumer_tick()
            except Exception:
                logger.error("Settlement consumer error", exc_info=True)

    async def _settlement_consumer_tick(self):
        """Fetch pending settlement items, process them, and mark final status."""
        items = self.storage.get_pending_settlements(limit=10)
        if not items:
            return

        for item in items:
            try:
                await self._process_settlement_item(item)
                await self._enqueue_write(
                    self.storage.mark_settlement_processed, item["id"], "processed"
                )
            except Exception as exc:
                logger.error(
                    f"SETTLEMENT_ITEM_FAILED id={item['id']} type={item['item_type']}: {exc}"
                )
                await self._enqueue_write(
                    self.storage.mark_settlement_processed, item["id"], "failed"
                )

    async def _process_settlement_item(self, item: dict):
        """Route a settlement queue item to the appropriate handler."""
        item_type = item.get("item_type", "")
        if item_type == "soft_threshold":
            await self._handle_settlement_soft_threshold(item)
        elif item_type == "settle_request":
            await self._handle_settlement_request(item)
        elif item_type == "settlement_confirmation":
            await self._handle_settlement_confirmation(item)
        else:
            logger.warning(f"SETTLEMENT_UNKNOWN_TYPE type={item_type} id={item.get('id')}")

    def _run_netting_cycle_if_due(self, now: Optional[float] = None) -> int:
        """Run the settlement netting cycle if its interval has elapsed."""
        now = time.time() if now is None else now
        netting_interval = float(self._get_settlement_config().get("netting_interval", 3600))
        if getattr(self, "_last_netting", 0.0) and (now - self._last_netting) <= netting_interval:
            return 0
        try:
            from ..commerce.netting import run_netting_cycle
            queued = run_netting_cycle(self)
            self._last_netting = now
            if queued:
                logger.info(f"NETTING_CYCLE queued={queued}")
            return queued
        except Exception as exc:
            logger.error(f"Netting cycle failed: {exc}")
            return 0

    async def _peer_heartbeat_sweep(self, peers, now):
        for peer in peers:
            last_seen = self._peer_last_activity.get(peer.node_id)
            if last_seen is None:
                # New peer — initialize activity to now, evaluate next cycle
                self._peer_last_activity[peer.node_id] = now
                continue
            silence = now - last_seen

            if silence > self._peer_dead_timeout:
                # Dead: no activity of any kind for too long
                logger.warning(f"Removing dead peer {peer.node_id[:16]} (silent {silence:.0f}s)")
                await self._enqueue_write(self.storage.remove_peer, peer.node_id)
                await self._pool.remove(peer.node_id)
                self._peer_last_activity.pop(peer.node_id, None)
                continue

            if silence > self._heartbeat_silence_threshold:
                # Silent: send dedicated heartbeat
                logger.debug(f"HB_SEND to={peer.node_id[:16]} silence={silence:.0f}s")
                msg = self._sign(Heartbeat(
                    node_id=self.node_info.node_id,
                    timestamp=time.time(),
                    version=__version__,
                ))
                try:
                    h, p = self.resolve_peer(peer.node_id, peer.host, peer.port)
                    resp = await self._pool.send(peer.node_id, h, p, msg)
                except Exception:
                    logger.debug(f"HB_SEND_FAIL to={peer.node_id[:16]}", exc_info=True)
                    continue
                if isinstance(resp, Heartbeat) and verify_message(resp) and verify_node_id(resp):
                    self._peer_last_activity[peer.node_id] = time.monotonic()
                    logger.debug(f"HB_SEND_OK to={peer.node_id[:16]}")

                    # v0.17.0: Auto-populate address book cached tier
                    await self._enqueue_write_proto(
                        self.storage.upsert_address,
                        peer.node_id, "cached", None,
                        peer.host, peer.port,
                        getattr(peer, 'sidecar_port', 0)
                    )

                    # v0.17.0: Try to push mail to this peer now that we know they are up
                    h, p = self.resolve_peer(peer.node_id, peer.host, peer.port)
                    await self._sync.push_to_peer(peer.node_id, h, p)

                    # H14: Version gating and update notifications
                    if resp.min_protocol_version and _parse_version(resp.min_protocol_version) > _parse_version(__version__):
                        if not self._version_gated:
                            self._version_gated = True
                            logger.warning(
                                f"Node version {__version__} is below network minimum {resp.min_protocol_version} "
                                f"— skills suspended. Update: knarr upgrade"
                            )
                            # v0.33.0: node.version_blocked
                            if self.bus:
                                self.bus.emit("node.version_blocked", required_version=resp.min_protocol_version, current_version=__version__, identity=self.node_info.node_id)
                    elif self._version_gated and resp.min_protocol_version:
                        self._version_gated = False
                        logger.info(f"Node version {__version__} meets minimum {resp.min_protocol_version} — skills resumed")

                    if resp.version and _parse_version(resp.version) > _parse_version(__version__):
                        if not hasattr(self, '_notified_version') or self._notified_version != resp.version:
                            self._notified_version = resp.version
                            logger.info(f"New knarr version available: {resp.version} (running {__version__})")
                            # v0.33.0: node.upgrade_available
                            if self.bus:
                                self.bus.emit("node.upgrade_available", current_version=__version__, available_version=resp.version, identity=self.node_info.node_id)

    async def _heartbeat_tick(self):
        """Single heartbeat maintenance cycle. Separated for resilience.

        v0.41.0: flush_outbox, pull_from_correspondents, and _peer_heartbeat_sweep
        have been extracted to independent background task loops.
        """
        await self._enqueue_write(self.storage.cleanup_expired_jobs)
        await self._sync.cleanup()

        # V015: Plugin tick
        peers = self.storage.get_peers()
        health = NodeHealth(
            event_loop_lag_ms=getattr(self, '_loop_lag_ema', 0.0),
            active_connections=self._active_connections,
            max_connections=MAX_CONCURRENT_CONNECTIONS,
            write_queue_depth=self._write_queue.qsize(),
            peer_count=len(peers),
            uptime_seconds=time.monotonic() - self._start_time,
        )
        await self._plugins.on_tick(peers, health)
        if self.bus:
            _bus_fired = self.bus.tick()
            if _bus_fired and self._config.get("node", {}).get("event_bus_debug", False):
                logger.info(f"BUS_TICK_FIRED fired={_bus_fired}")

        await self._pool.evict_idle(self._connection_idle_timeout)

        # Netting cycle (runs hourly, checks bilateral positions against soft threshold)
        self._run_netting_cycle_if_due()

    async def _event_loop_watchdog(self):
        """Detects event loop blocking by measuring scheduling latency."""
        while self._running:
            before = time.monotonic()
            await asyncio.sleep(2.0)
            elapsed = time.monotonic() - before
            if elapsed > 4.0:
                logger.warning(f"Event loop blocked for {elapsed - 2.0:.1f}s")
                # v0.33.0: node.event_loop_blocked
                if self.bus:
                    self.bus.emit("node.event_loop_blocked", blocked_seconds=round(elapsed - 2.0, 1), identity=self.node_info.node_id)

    async def _stale_task_watchdog(self):
        """Reaps tasks stuck in 'accepted' longer than 2x their timeout."""
        while self._running:
            await asyncio.sleep(60)
            try:
                now = time.time()
                conn = self.storage._get_conn()
                cursor = conn.execute(
                    "SELECT task_id, timeout_ms, created_at FROM tasks WHERE status = 'accepted'"
                )
                for task_id, timeout_ms, created_at in cursor.fetchall():
                    max_age = (timeout_ms / 1000.0) * 2
                    age_seconds = now - created_at
                    if age_seconds > max_age:
                        await self._enqueue_write(
                            self.storage.update_task_status, task_id, "expired"
                        )
                        logger.warning(f"Reaped stale task {task_id[:8]} (accepted {age_seconds:.0f}s ago)")
                        # v0.33.0: task.timeout
                        if self.bus:
                            # Retrieve skill_name from task record
                            task_row = conn.execute("SELECT skill_name FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                            _skill = task_row[0] if task_row else "unknown"
                            self.bus.emit("task.timeout", skill_name=_skill, task_id=task_id, age_seconds=round(age_seconds, 1), identity=self.node_info.node_id)
            except Exception as e:
                logger.debug(f"Stale task watchdog error: {e}")

    async def _mail_ttl_cleanup(self):
        """Purge expired mail and run per-bucket GC. All writes go through the writer queue."""
        gc_counter = 0
        while self._running:
            await asyncio.sleep(60)
            try:
                now = time.time()
                await self._enqueue_write(self.storage.purge_expired_mail, now)

                # v0.29.1: Per-bucket GC — runs every 10 minutes (every 10th tick)
                gc_counter += 1
                if gc_counter >= 10:
                    gc_counter = 0
                    await self._run_bucket_gc()
            except Exception as e:
                logger.debug(f"Mail TTL cleanup error: {e}")

    async def _run_bucket_gc(self):
        """Per-bucket mail GC + execution log retention."""
        mail_cfg = self._config.get("mail", {}).get("buckets", {})

        # Jobreport: default 48h retention
        jr_ttl = float(mail_cfg.get("jobreport", {}).get("ttl_hours", 48)) * 3600
        await self._enqueue_write(self.storage.purge_bucket_by_age, "mail_jobreport", jr_ttl)

        # System: default 1h retention (aggressive — already dispatched)
        sys_ttl = float(mail_cfg.get("system", {}).get("ttl_hours", 1)) * 3600
        await self._enqueue_write(self.storage.purge_bucket_by_age, "mail_system", sys_ttl)

        # Per-bucket max rows trim
        for bucket_name, default_max in [("inbox", 10000), ("jobreport", 50000), ("system", 5000)]:
            max_rows = int(mail_cfg.get(bucket_name, {}).get("max_messages", default_max))
            await self._enqueue_write(self.storage.trim_bucket, f"mail_{bucket_name}", max_rows)

        # Execution log: configurable retention
        node_cfg = self._config.get("node", {})
        retention_mode = node_cfg.get("log_retention", "internal")
        if retention_mode == "internal":
            log_ttl_hours = float(node_cfg.get("log_retention_hours", 168))
            await self._enqueue_write(self.storage.purge_execution_log_by_age, log_ttl_hours * 3600)

        # Async jobs: delete old expired
        retention_days = int(node_cfg.get("housekeeping_retention_days", 7))
        await self._enqueue_write(self.storage.delete_old_expired_jobs, retention_days)

    def _get_skill_ttl(self) -> int:
        """Scale skill TTL with network size."""
        peer_count = len(self.storage.get_peers())
        if peer_count < 20:
            return 1800    # 30 min — small network
        elif peer_count < 50:
            return 3600    # 1 hour — medium
        else:
            return 5400    # 90 min — large cluster

    def _get_announce_hops(self) -> int:
        """Scale max announce hops with network size."""
        peer_count = len(self.storage.get_peers())
        if peer_count >= 50:
            return 3
        return MAX_ANNOUNCE_HOPS  # 2

    async def _republish_loop(self):
        while self._running:
            jitter = random.uniform(-30, 30)
            await asyncio.sleep(300 + jitter)
            await self._reannounce_all()
            self.refresh_node_meta()

    def _get_prune_timeout(self) -> float:
        """Scale prune timeout with network size. Larger networks need more patience."""
        peer_count = len(self.storage.get_peers())
        if peer_count < 20:
            return PEER_DEAD_TIMEOUT           # 300s — small network
        elif peer_count < 50:
            return PEER_DEAD_TIMEOUT * 1.5     # 450s — medium cluster
        else:
            return PEER_DEAD_TIMEOUT * 2       # 600s — large cluster

    async def _prune_loop(self):
        while self._running:
            await asyncio.sleep(60)
            # Sync in-memory liveness to DB
            now = time.monotonic()
            prune_timeout = self._get_prune_timeout()
            alive = [nid for nid, t in list(self._peer_last_activity.items())
                     if now - t < prune_timeout]
            if alive:
                await self._enqueue_write_proto(self.storage.touch_peers, alive)

            current_count = len(self.storage.get_peers())
            logger.debug(f"PRUNE_SYNC alive={len(alive)} tracked={len(self._peer_last_activity)} peers_db={current_count} timeout={prune_timeout}")

            pruned_skills = await self._enqueue_write(self.storage.prune_stale_skills)
            if pruned_skills:
                logger.info(f"PRUNE_SKILLS removed={pruned_skills}")

            # Peer floor check — never prune below minimum
            # v0.33.0: C-track — min_peers from config
            min_peer_floor = max(1, int(self._config.get("network", {}).get("min_peers", MIN_PEER_FLOOR)))
            if current_count <= min_peer_floor:
                logger.warning(f"PRUNE_SKIP peer_count={current_count} at_or_below_floor={min_peer_floor}")
            else:
                pruned = await self._enqueue_write(self.storage.prune_stale_peers, prune_timeout, self.node_info.node_id)
                if pruned:
                    new_count = len(self.storage.get_peers())
                    logger.info(f"PRUNE_PEERS removed={pruned} before={current_count} after={new_count}")
                    # v0.33.0: peer.removed (count-based since prune_stale_peers returns count)
                    if self.bus:
                        self.bus.emit("peer.removed", node_id="batch", reason="stale", peer_count=new_count, identity=self.node_info.node_id)
                    if current_count > 0 and pruned / current_count > 0.2:
                        logger.warning(f"PRUNE_CASCADE_RISK dropped {pruned}/{current_count} ({pruned/current_count:.0%}) in one cycle")

            await self._enqueue_write(self.storage.prune_completed_tasks)

    async def _auto_upgrade_loop(self):
        """Check for and install new versions. Opt-in only."""
        if not self._config.get("node", {}).get("auto_upgrade", False):
            logger.debug("UPGRADE disabled (auto_upgrade not set)")
            return  # Opt-in only
        logger.info("UPGRADE loop started (check interval=300s)")
        while self._running:
            await asyncio.sleep(300)  # Check every 5 min
            if not self._notified_version:
                logger.debug("UPGRADE tick: no notified_version yet")
                continue
            if _parse_version(self._notified_version) <= _parse_version(__version__):
                logger.debug(f"UPGRADE tick: notified={self._notified_version} <= current={__version__}, skipping")
                continue
            logger.info(f"UPGRADE detected: notified={self._notified_version} > current={__version__}")
            # Wait for zero active tasks (max 1 hour)
            drained = False
            for i in range(720):
                if self._active_workers == 0:
                    drained = True
                    break
                if i % 60 == 0:
                    logger.debug(f"UPGRADE drain: waiting, active_workers={self._active_workers}")
                await asyncio.sleep(5)
            if not drained:
                logger.info("UPGRADE deferred: active tasks still running after 1h")
                continue
            self._upgrading = True
            try:
                from .upgrade import check_and_upgrade, backup_config, verify_installation, rollback_installation, cleanup_old_backups, get_latest_version

                loop = asyncio.get_event_loop()

                # Fix #25: Avoid backup cycle if GitHub release isn't out yet
                logger.info("UPGRADE checking GitHub releases API...")
                latest = await loop.run_in_executor(None, get_latest_version)
                if not latest:
                    logger.info("UPGRADE abort: could not fetch latest version from GitHub")
                    self._upgrading = False
                    continue
                if _parse_version(latest) <= _parse_version(__version__):
                    logger.info(f"UPGRADE abort: GitHub latest={latest} <= current={__version__} (release not published yet?)")
                    self._upgrading = False
                    continue

                logger.info(f"UPGRADE proceeding: {__version__} -> {latest}")
                config_dir = self._config.get("_config_dir", "")
                data_dir = self._config.get("_data_dir", config_dir)
                backup_dir = backup_config(config_dir, __version__, data_dir=data_dir)
                if not backup_dir:
                    logger.warning("UPGRADE abort: backup failed")
                    self._upgrading = False
                    continue
                logger.info(f"UPGRADE backup created: {backup_dir}")

                success = await loop.run_in_executor(None, check_and_upgrade, latest)
                if success:
                    # H14: Verify installation and rollback if necessary
                    if not verify_installation(latest):
                        logger.error("UPGRADE verification failed, rolling back")
                        rollback_installation(backup_dir, config_dir, data_dir=data_dir)
                        continue

                    logger.info(f"UPGRADE complete: {__version__} -> {latest}, requesting restart...")

                    # Cleanup old backups
                    retention = int(self._config.get("node", {}).get("backup_retention_days", 7))
                    cleanup_old_backups(config_dir, retention)

                    self._restart_requested = True
                    self._running = False
                    # Signal the main event loop to shut down cleanly.
                    # Without this, main.py's shutdown.wait() blocks forever
                    # and the node zombifies (sockets open, tasks dead).
                    if self._shutdown_event:
                        self._shutdown_event.set()
                    else:
                        import signal as _sig
                        os.kill(os.getpid(), _sig.SIGTERM)
                else:
                    logger.warning("UPGRADE installation failed (check_and_upgrade returned False), will retry next cycle")
                    # v0.33.0: node.upgrade_failed
                    if self.bus:
                        self.bus.emit("node.upgrade_failed", from_version=__version__, to_version=latest, error="check_and_upgrade returned False", identity=self.node_info.node_id)
            except Exception as e:
                logger.error(f"UPGRADE error: {e}", exc_info=True)
                # v0.33.0: node.upgrade_failed
                if self.bus:
                    self.bus.emit("node.upgrade_failed", from_version=__version__, to_version=getattr(self, '_notified_version', ''), error=str(e), identity=self.node_info.node_id)
            finally:
                if not self._restart_requested:
                    self._upgrading = False


    async def _refresh_balances(self):
        """Refresh token and SOL balances from Solana RPC. Cached 60s."""
        if not self._wallet or not self._token_mint:
            return
        now = time.time()
        if now - self._balance_last_refresh < 60:
            return
        from ..core.solana_rpc import get_token_balance, get_sol_balance
        rpc_url = self._rpc_url  # None = default
        kwargs = {"rpc_url": rpc_url} if rpc_url else {}
        self._token_balance = await get_token_balance(self._wallet, self._token_mint, **kwargs)
        self._sol_balance = await get_sol_balance(self._wallet, **kwargs)
        self._balance_last_refresh = now
        logger.debug(f"Balance refresh: $KNARR={self._token_balance} SOL={self._sol_balance}")

    async def _balance_refresh_loop(self):
        """Periodic balance refresh (every 300s)."""
        while self._running:
            try:
                await self._refresh_balances()
            except Exception as e:
                logger.debug(f"Balance refresh error: {e}")
            await asyncio.sleep(300)
