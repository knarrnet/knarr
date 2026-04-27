import asyncio
import dataclasses
import importlib.resources
import importlib.util
import json
import logging
import sys
import time
import tomllib
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Type, Any

from knarr.core.messages import Message, PluginMessage
from knarr.core.models import NodeInfo

log = logging.getLogger(__name__)


# A-02: Protocol bus topic prefixes — route to shared bus.
# Everything else goes to the identity bus.
_PROTOCOL_EVENT_PREFIXES = ("peer.", "node.", "security.", "cache.")
_PROTOCOL_EVENT_NAMES = {"skill.registered", "skill.removed"}


def _is_relative_to(path: Path, prefix: Path) -> bool:
    try:
        path.relative_to(prefix)
        return True
    except ValueError:
        return False


def _knarr_install_prefix() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    try:
        import knarr
        package_file = getattr(knarr, "__file__", "")
        if package_file:
            return Path(package_file).resolve().parent
    except Exception:
        pass
    return Path(__file__).resolve().parents[1]


def is_protocol_event(event_type: str) -> bool:
    """A-02: Classify event topic as protocol (shared) vs identity (scoped)."""
    event_type = str(event_type or "")
    if event_type in _PROTOCOL_EVENT_NAMES:
        return True
    return event_type.startswith(_PROTOCOL_EVENT_PREFIXES)


class ScopedSubscriber:
    """A-02: Read from a shared protocol bus plus a single identity bus."""

    def __init__(self, *subs):
        self._subs = [sub for sub in subs if sub is not None]
        self._buffer: list = []  # TP-9: buffer for simultaneous events

    async def next(self) -> dict:
        # TP-9: Return buffered events first before waiting for new ones
        if self._buffer:
            return self._buffer.pop(0)
        tasks = [asyncio.create_task(sub.next()) for sub in self._subs]
        if not tasks:
            await asyncio.Event().wait()
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # TP-12: Cancel pending and await to avoid leaked coroutines
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # TP-9: Collect ALL done results, return first, buffer rest
        results = [t.result() for t in done]
        first = results[0]
        if len(results) > 1:
            self._buffer.extend(results[1:])
        return first

    def poll(self) -> list:
        events = []
        for sub in self._subs:
            events.extend(sub.poll())
        return sorted(events, key=lambda event: (event.get("ts", 0.0), event.get("event_id", "")))


class ScopedEventBus:
    """A-02: Route protocol topics to the shared bus and everything else to one identity bus."""

    def __init__(self, protocol_bus, identity_bus):
        self.protocol_bus = protocol_bus
        self.identity_bus = identity_bus

    def emit(self, event_type: str, valid_from: float = None,
             valid_until: float = None, **fields) -> str:
        bus = self.protocol_bus if is_protocol_event(event_type) else self.identity_bus
        return bus.emit(event_type, valid_from=valid_from, valid_until=valid_until, **fields)

    def subscribe(self, *patterns: str) -> ScopedSubscriber:
        return ScopedSubscriber(
            self.protocol_bus.subscribe(*patterns),
            self.identity_bus.subscribe(*patterns),
        )

    def tick(self) -> int:
        return int(self.protocol_bus.tick()) + int(self.identity_bus.tick())

    def cancel(self, event_id: str) -> bool:
        return bool(self.identity_bus.cancel(event_id) or self.protocol_bus.cancel(event_id))

    def unsubscribe(self, subscriber) -> None:
        """TP-13: Unsubscribe from both internal buses."""
        if hasattr(self.protocol_bus, "unsubscribe"):
            self.protocol_bus.unsubscribe(subscriber)
        if hasattr(self.identity_bus, "unsubscribe"):
            self.identity_bus.unsubscribe(subscriber)

    def get_metrics(self) -> dict:
        """Return combined metrics from both buses."""
        return {
            "protocol": self.protocol_bus.get_metrics() if hasattr(self.protocol_bus, "get_metrics") else {},
            "identity": self.identity_bus.get_metrics() if hasattr(self.identity_bus, "get_metrics") else {},
        }


@dataclasses.dataclass
class NodeHealth:
    """Read-only snapshot of node health, passed to on_tick for adaptive behavior."""
    event_loop_lag_ms: float       # EMA of scheduling latency
    active_connections: int        # current count
    max_connections: int           # MAX_CONCURRENT_CONNECTIONS
    write_queue_depth: int         # self._write_queue.qsize()
    peer_count: int                # len(peers)
    uptime_seconds: float


class PluginHooks:
    """
    Base class for Knarr plugins to override specific hooks in the node's lifecycle.
    Plugins should inherit from this and implement the async methods they need.
    Default implementations allow all operations and do nothing for maintenance hooks.
    """

    async def on_connect(self, peer_ip: str) -> bool:
        """Called on TCP accept, BEFORE deserialization. Cheapest rejection point.
        Return False to close the connection immediately."""
        return True

    async def on_inbound(self, msg: Message, peer_ip: str) -> bool:
        """
        Called after message deserialization and signature verification, but before
        it is processed by the core node logic.
        Return False to silently drop the message.
        """
        return True

    async def on_outbound(self, msg: Message, peer: NodeInfo) -> bool:
        """
        Called before sending a message to a peer.
        Return False to suppress the message from being sent.
        """
        return True

    async def on_tick(self, peers: List[NodeInfo], health: NodeHealth) -> None:
        """
        Called periodically (once per heartbeat cycle ~10s) for maintenance tasks.
        Receives current node health snapshot for adaptive behavior.
        """
        pass

    async def on_query(self, query_type: str, value: str) -> List[Dict[str, Any]]:
        """Called during query resolution. Return provider records to merge into results.
        Each dict must have: node_id, host, port, skill_key. Optional: sidecar_port."""
        return []

    async def on_task_complete(self, skill_name: str, job_id: str,
                              caller_node_id: str, result_data: Any,
                              wall_ms: int) -> None:
        """Called after a task execution completes (success or failure).
        result_data is None on failure."""
        pass

    async def on_mail_received(self, msg_type: str, from_node: str,
                               to_node: str, body: Any,
                               session_id: Optional[str]) -> None:
        """Called after an incoming mail item is stored in the inbox."""
        pass

    async def on_shutdown(self) -> None:
        """
        Called when the node is shutting down. Use this to flush state or clean up.
        """
        pass

    # v0.36.0: Settlement hooks
    async def on_settlement_review(self, prepared_tx: dict) -> Optional[dict]:
        """Called when node has prepared a settlement for authority review.

        prepared_tx is a signed Document (settlement_prepared, signed by #key-1).

        Return countersigned Document to approve (call ctx.sign_document on it).
        Return None to reject (settlement skipped this cycle).

        Default: auto-approve (hotwire) — returns prepared_tx unchanged.
        This means unsigned settlements in the degenerate case.
        """
        return prepared_tx

    async def on_inbound_settlement(self, settle_request: dict) -> bool:
        """Called when counterparty's settle_request arrives and passes validation.

        settle_request contains: dual-signed settlement proposal, positions, amount.
        Node has already validated both signatures and run sanity check.

        Return True to accept (zero ledger, send confirmation).
        Return False to reject (send rejection with reason).

        Default: accept.
        """
        return True


class PluginContext:
    """Provides plugins with access to node information and utilities.

    Supports two construction paths:
      - Legacy: PluginContext(node_id=..., plugin_dir=..., get_peers=..., ...)
      - v0.48.0 D-007: PluginContext(node=node, plugin_name="...", send_fn=..., data_dir=...)
    """

    def __init__(
        self,
        node_id: str = "",
        plugin_dir=None,
        config_dir=None,
        config=None,
        get_peers=None,
        send_to_peer=None,
        send_fire_forget=None,
        delivery_cb=None,
        log=None,
        *,
        state_dir=None,
        group_engine=None,
        storage_path=None,
        register_mail_handler=None,
        send_mail=None,
        register_egress_material=None,
        register_capability=None,
        vault_get=None,
        vault_set=None,
        update_cache=None,
        subscribe_events=None,
        emit_event=None,
        bus=None,
        sign_document=None,
        query_receipts=None,
        query_prepaid_balance=None,
        economy_config=None,
        get_plugin=None,
        remove_peer=None,
        upsert_address=None,
        push_to_peer=None,
        # v0.48.0 D-007: node= factory path
        node=None,
        plugin_name=None,
        send_fn=None,
        data_dir=None,
    ):
        if node is not None:
            # D-007 Phase D: node-centric factory path
            self._node = node
            self.node_id = node.node_info.node_id if not node_id else node_id
            self.plugin_dir = data_dir
            self.config_dir = config_dir
            self.config = dict(config or {})
            self._debug = bool(self.config.get("debug", False))
            self.get_peers = get_peers or (lambda: [])
            self.send_to_peer = send_fn
            self.send_fire_forget = send_fn
            self.delivery_cb = delivery_cb
            self.log = log or logging.getLogger(f"plugin.{plugin_name or 'unknown'}")
            self.state_dir = state_dir
            self.group_engine = group_engine
            self.storage_path = storage_path
            self.register_mail_handler = register_mail_handler
            self.send_mail = send_mail
            self.register_egress_material = register_egress_material
            self._register_capability_cb = register_capability or getattr(node, "register_capability", None)
            self.vault_get = vault_get
            self.vault_set = vault_set
            self.update_cache = update_cache
            self.subscribe_events = subscribe_events
            self.emit_event = emit_event
            self.bus = bus if bus is not None else getattr(node, "bus", None)
            self.sign_document = sign_document
            self.query_receipts = query_receipts
            self.query_prepaid_balance = query_prepaid_balance
            self.economy_config = economy_config
            self.get_plugin = get_plugin
            self.remove_peer = remove_peer or (lambda nid: node.storage.remove_peer(nid))
            self.upsert_address = upsert_address or (
                lambda nid, host, port: node.storage.upsert_address(nid, host, port)
            )
            self.push_to_peer = push_to_peer or send_fn
        else:
            # Legacy positional/keyword construction (PluginLoader path)
            self._node = None
            self.node_id = node_id
            self.plugin_dir = plugin_dir
            self.config_dir = config_dir if config_dir is not None else plugin_dir
            self.config = dict(config or {})
            self._debug = bool(self.config.get("debug", False))
            self.get_peers = get_peers
            self.send_to_peer = send_to_peer
            self.send_fire_forget = send_fire_forget
            self.delivery_cb = delivery_cb
            self.log = log or logging.getLogger("plugin")
            self.state_dir = state_dir
            self.group_engine = group_engine
            self.storage_path = storage_path
            self.register_mail_handler = register_mail_handler
            self.send_mail = send_mail
            self.register_egress_material = register_egress_material
            self._register_capability_cb = register_capability
            self.vault_get = vault_get
            self.vault_set = vault_set
            self.update_cache = update_cache
            self.subscribe_events = subscribe_events
            self.emit_event = emit_event
            self.bus = bus
            self.sign_document = sign_document
            self.query_receipts = query_receipts
            self.query_prepaid_balance = query_prepaid_balance
            self.economy_config = economy_config
            self.get_plugin = get_plugin
            self.remove_peer = remove_peer                     # v0.48.0 D-007
            self.upsert_address = upsert_address               # v0.48.0 D-007
            self.push_to_peer = push_to_peer                   # v0.48.0 D-007
        # KAD-06: sign_bytes — raw bytes signing for KAD record signatures.
        # Set to None here; wired in node.py after plugin load.
        if not hasattr(self, "sign_bytes"):
            self.sign_bytes = None  # Callable: (bytes) -> (signature_bytes, pubkey_hex)

    def register_capability(self, name: str, getter, default: Any = None) -> None:
        callback = getattr(self, "_register_capability_cb", None)
        if callable(callback):
            callback(name, getter, default)

    async def query_plugin(
        self,
        node_id,
        host,
        port,
        plugin_name,
        action,
        payload,
        timeout=5.0,
        trace_id: str = "",  # A-02: explicit correlation ID for request tracing
    ) -> Optional[dict]:
        """Send a fire-and-forget plugin RPC and wait for its correlated response."""
        node = getattr(self, "_node", None)
        if node is None:
            return None
        return await node.query_plugin(
            node_id,
            host,
            port,
            plugin_name,
            action,
            payload,
            timeout=timeout,
            trace_id=trace_id,
        )

    def current_identity_name(self) -> Optional[str]:
        """Return the scoped identity name, or None outside identity context."""
        try:
            from .node import _current_identity
        except Exception:
            return None
        identity = _current_identity.get()
        return getattr(identity, "name", None) if identity is not None else None

    def get_economy_stats(self) -> dict:
        """C-06: Return economy stats from node storage directly (local bypass).

        Avoids circular HTTP round-trip when node is available.
        Falls back to None when node or storage is not available — caller
        should then fall back to HTTP API.
        """
        try:
            if self._node is not None:
                storage = getattr(self._node, "storage", None)
                if storage is not None:
                    fn = getattr(storage, "get_economy_stats", None)
                    if callable(fn):
                        return fn()
        except Exception:
            pass
        return None


class PluginLoader:
    """
    Discovers, loads, and manages Knarr plugins from the specified plugin directories.
    """
    def __init__(self, config_dir: Path, get_peers_cb: Optional[Callable] = None, send_to_peer_cb: Optional[Callable] = None, node_id: str = "", delivery_cb: Optional[Callable] = None, send_fire_forget_cb: Optional[Callable] = None, register_mail_handler_cb: Optional[Callable] = None, send_mail_cb: Optional[Callable] = None, register_egress_material_cb: Optional[Callable] = None, register_capability_cb: Optional[Callable] = None, vault_get_cb: Optional[Callable] = None, vault_set_cb: Optional[Callable] = None, storage_path: Optional[str] = None, update_cache_cb: Optional[Callable] = None, subscribe_events_cb: Optional[Callable] = None, emit_event_cb: Optional[Callable] = None, bus: Optional[Any] = None, data_dir: Optional[Path] = None):
        self._plugin_root = config_dir / "plugins"
        self._state_root = data_dir / "plugin_state" if data_dir else None
        self._get_peers_cb = get_peers_cb
        self._send_to_peer_cb = send_to_peer_cb
        self._send_fire_forget_cb = send_fire_forget_cb
        self._register_mail_handler_cb = register_mail_handler_cb
        self._send_mail_cb = send_mail_cb
        self._register_egress_material_cb = register_egress_material_cb
        self._register_capability_cb = register_capability_cb
        self._vault_get_cb = vault_get_cb
        self._vault_set_cb = vault_set_cb
        self._storage_path = storage_path
        self._update_cache_cb = update_cache_cb
        self._subscribe_events_cb = subscribe_events_cb   # v0.32.0
        self._emit_event_cb = emit_event_cb               # v0.32.0
        self._bus = bus                                    # v0.33.0
        self._node_id = node_id
        self._delivery_cb = delivery_cb
        self.plugins: List[PluginHooks] = []
        self._name_to_plugin: Dict[str, PluginHooks] = {}  # v0.46.0: name → instance

    def _find_package_plugin_root(self) -> Optional[Path]:
        """T2-08 (v0.56.0): locate the package-shipped
        plugins directory via the knarr package's __file__. Avoids importlib.resources
        namespace-package magic — direct filesystem path is more robust.
        """
        try:
            import knarr
            pkg_path = Path(knarr.__file__).resolve().parent / "plugins"
            if pkg_path.is_dir():
                return pkg_path
        except Exception as e:
            log.debug(f"package plugin root lookup failed: {e}")
        return None

    def _scan_plugin_source(
        self,
        root: Optional[Path],
        source_label: str,
        skip_names: Optional[set] = None,
    ) -> tuple:
        """Scan a plugin root directory and return ((priority, name, path, cfg), seen_names).

        ``skip_names`` is a set of plugin names already claimed by a
        higher-precedence source and must not be re-added.
        """
        entries: list = []
        seen: set = set()
        if root is None or not root.is_dir():
            return entries, seen
        source_prefix = (
            _knarr_install_prefix()
            if source_label == "package"
            else root.parent.resolve()
        )
        # Adversary #4 fix (v0.56.0): case-insensitive seen_names comparison
        # prevents dual loading when the same plugin appears with different
        # casing in two sources (e.g., `MyPlugin` in node-local and `myplugin`
        # in the package fallback). Without this, both would load and register
        # duplicate hooks / corrupt plugin state.
        skip = {s.lower() for s in (skip_names or set())}
        for plugin_path in root.iterdir():
            if not plugin_path.is_dir():
                continue
            resolved_plugin_path = plugin_path.resolve()
            if not _is_relative_to(resolved_plugin_path, source_prefix):
                log.warning(
                    "PLUGIN_ROOT_REJECTED source=%s nominal=%s resolved=%s",
                    source_label, plugin_path, resolved_plugin_path,
                )
                continue
            toml_path = plugin_path / "plugin.toml"
            if not toml_path.is_file():
                log.warning(f"Skipping {plugin_path.name}: plugin.toml not found.")
                continue
            try:
                plugin_config = tomllib.loads(toml_path.read_text())
            except Exception as e:
                log.warning(f"Failed to parse plugin.toml for {plugin_path.name}: {e}")
                continue
            plugin_name = plugin_config.get("name", plugin_path.name)
            plugin_name_lc = plugin_name.lower()
            if plugin_name_lc in skip:
                log.debug(
                    "PLUGIN_LOADER_FALLBACK source=%s plugin=%s (skipped — node_local wins)",
                    source_label, plugin_name,
                )
                continue
            # Post-review fix (v0.56.0, Sonnet subagent): guard against
            # intra-source case-insensitive duplicates. Without this check,
            # two subdirectories in the SAME source with plugin.toml names
            # differing only in case (e.g., `Kademlia/` + `kademlia/` on a
            # case-sensitive filesystem) would BOTH be added to `entries`
            # because `skip` only tracks cross-source dedup, not intra-source.
            # `seen` accumulates across this loop but was never consulted
            # against itself — fix here.
            if plugin_name_lc in seen:
                log.warning(
                    "PLUGIN_LOADER_INTRASOURCE_DUP source=%s plugin=%s (already loaded this scan)",
                    source_label, plugin_name,
                )
                continue
            seen.add(plugin_name_lc)
            log.debug(
                "PLUGIN_LOADER_FALLBACK source=%s plugin=%s",
                source_label, plugin_name,
            )
            priority = int(plugin_config.get("priority", 10))
            entries.append((priority, plugin_path.name.lower(), plugin_path, plugin_config))
        return entries, seen

    def _check_plugin_drift(
        self,
        plugin_name: str,
        local_path: Path,
        package_path: Path,
        local_config: Path,
        package_config: Path,
    ) -> None:
        """B-06 (v0.58.0): compare handler source between local and package.

        If the handler source bytes differ, emit one WARNING with the plugin
        name and short hash prefixes of both sources. Load behavior is
        unchanged — local still wins.
        """
        def _handler_file(plugin_dir: Path) -> Optional[Path]:
            toml = plugin_dir / "plugin.toml"
            if not toml.is_file():
                return None
            try:
                import tomllib as _t
                cfg = _t.loads(toml.read_text())
                handler = cfg.get("handler", "")
                if ":" in handler:
                    mod_name = handler.split(":")[0]
                    return plugin_dir / f"{mod_name}.py"
            except Exception:
                pass
            return None

        local_handler = _handler_file(local_path)
        pkg_handler = _handler_file(package_path)

        if local_handler is None or pkg_handler is None:
            return
        if not local_handler.is_file() or not pkg_handler.is_file():
            return

        try:
            local_bytes = local_handler.read_bytes()
            pkg_bytes = pkg_handler.read_bytes()
        except OSError:
            return

        if local_bytes == pkg_bytes:
            return

        import hashlib
        local_hash = hashlib.sha256(local_bytes).hexdigest()[:8]
        pkg_hash = hashlib.sha256(pkg_bytes).hexdigest()[:8]

        log.warning(
            "PLUGIN_DRIFT name=%s local_hash=%s package_hash=%s (local wins)",
            plugin_name, local_hash, pkg_hash,
        )

    def load_plugins(self) -> None:
        """
        Scans for and loads plugins. Logs warnings for failures but continues startup.

        T2-08 (v0.56.0): When a plugin is not present
        in the node-local ``plugins/`` directory, fall back to the package-shipped copy
        under ``knarr/plugins/<name>``. Node-local always takes precedence when both
        exist. Missing-config errors (KeyError) skip the plugin with a warning instead
        of crashing the node.
        """
        config_dir = self._plugin_root.parent
        plugin_entries = []
        failed_required: List[str] = []

        # 1. Node-local plugin directories (operator-supplied, highest precedence)
        local_entries, seen_names = self._scan_plugin_source(
            self._plugin_root if self._plugin_root.is_dir() else None,
            source_label="node_local",
        )
        plugin_entries.extend(local_entries)

        # 2. Package fallback — shipped plugins under src/knarr/plugins/
        pkg_root = self._find_package_plugin_root()
        if pkg_root is not None and pkg_root != self._plugin_root:
            pkg_entries_all, _ = self._scan_plugin_source(
                pkg_root, source_label="package",
            )
            # B-06: drift detection — compare handler source when both exist
            local_by_name: dict[str, Path] = {}
            for _, _, ppath, _ in local_entries:
                toml_path = ppath / "plugin.toml"
                if toml_path.is_file():
                    try:
                        import tomllib as _tomllib
                        cfg = _tomllib.loads(toml_path.read_text())
                        pname = str(cfg.get("name", ppath.name))
                    except Exception:
                        pname = ppath.name
                    local_by_name[pname.lower()] = ppath

            for _, _, ppath, pcfg in pkg_entries_all:
                pname = str(pcfg.get("name", ppath.name))
                if pname.lower() in local_by_name:
                    self._check_plugin_drift(
                        plugin_name=pname,
                        local_path=local_by_name[pname.lower()],
                        package_path=ppath,
                        local_config=local_by_name[pname.lower()].parent / "plugin.toml",
                        package_config=ppath / "plugin.toml",
                    )

            pkg_entries = [
                entry for entry in pkg_entries_all
                if str(entry[3].get("name", entry[2].name)).lower() not in seen_names
            ]
            plugin_entries.extend(pkg_entries)

        if not plugin_entries:
            log.info(f"No plugins found (node_local={self._plugin_root}, package fallback checked).")
            return

        for _, _, plugin_path, plugin_config in sorted(plugin_entries, key=lambda item: (item[0], item[1])):
            handler_str = plugin_config.get("handler")
            if not handler_str:
                log.warning(f"Skipping {plugin_path.name}: 'handler' not specified in plugin.toml.")
                if plugin_config.get("required", False):
                    failed_required.append(plugin_config.get("name", plugin_path.name))
                continue

            missing_keys = self._missing_required_config_keys(plugin_config)
            if missing_keys:
                log.warning(
                    "PLUGIN_SKIPPED name=%s reason=missing_config keys=%s",
                    plugin_config.get("name", plugin_path.name),
                    ",".join(missing_keys),
                )
                continue

            module_name, class_name = handler_str.split(":")

            # V015-008: Confine handler path to plugin directory
            handler_file = (plugin_path / f"{module_name}.py").resolve()
            if not handler_file.is_relative_to(plugin_path.resolve()):
                log.warning(f"Skipping {plugin_path.name}: handler path escapes plugin directory.")
                if plugin_config.get("required", False):
                    failed_required.append(plugin_config.get("name", plugin_path.name))
                continue

            # Temporarily add plugin's directory to sys.path for import
            path_entry = str(plugin_path)
            sys.path.insert(0, path_entry)

            # V035-001: Pre-load sibling .py files with namespaced keys in
            # sys.modules so that `from X import Y` inside handler code
            # resolves to THIS plugin's copy, not a previously-loaded one.
            _sibling_names = {f.stem for f in plugin_path.glob("*.py")}
            _stashed_mods = {}
            for _sn in _sibling_names:
                if _sn in sys.modules:
                    _stashed_mods[_sn] = sys.modules.pop(_sn)
                # Pre-load this plugin's sibling module into sys.modules
                _sib_file = plugin_path / f"{_sn}.py"
                if _sib_file.is_file():
                    _sib_spec = importlib.util.spec_from_file_location(_sn, str(_sib_file))
                    if _sib_spec and _sib_spec.loader:
                        _sib_mod = importlib.util.module_from_spec(_sib_spec)
                        _sib_spec.loader.exec_module(_sib_mod)
                        sys.modules[_sn] = _sib_mod

            try:
                spec = importlib.util.spec_from_file_location(module_name, handler_file)
                if spec is None:
                    raise ImportError(f"Could not find module spec for {module_name}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                plugin_class = getattr(module, class_name)
                if not issubclass(plugin_class, PluginHooks):
                    raise TypeError(f"Plugin {class_name} in {module_name} must inherit from PluginHooks.")

                plugin_logger = logging.getLogger(f"knarr.plugin.{plugin_config['name']}")
                state_dir = plugin_path
                if self._state_root is not None:
                    state_dir = self._state_root / plugin_path.name
                    state_dir.mkdir(parents=True, exist_ok=True)

                plugin_context = PluginContext(
                    node_id=self._node_id,
                    plugin_dir=plugin_path,
                    config_dir=config_dir,
                    config=plugin_config.get("config", {}),
                    state_dir=state_dir,
                    get_peers=self._get_peers_cb,
                    send_to_peer=self._send_to_peer_cb,
                    send_fire_forget=self._send_fire_forget_cb or self._send_to_peer_cb,
                    delivery_cb=self._delivery_cb,
                    log=plugin_logger,
                    register_mail_handler=self._register_mail_handler_cb,
                    send_mail=self._send_mail_cb,
                    register_egress_material=self._register_egress_material_cb,
                    register_capability=self._register_capability_cb,
                    vault_get=self._vault_get_cb,
                    vault_set=self._vault_set_cb,
                    storage_path=self._storage_path,
                    update_cache=self._update_cache_cb,
                    subscribe_events=self._subscribe_events_cb,   # v0.32.0
                    emit_event=self._emit_event_cb,               # v0.32.0
                    bus=self._bus,                                 # v0.33.0
                )

                plugin_instance = plugin_class(plugin_context, config=plugin_config.get("config", {}))
                self.plugins.append(plugin_instance)
                self._name_to_plugin[plugin_config["name"]] = plugin_instance  # v0.46.0
                log.info(f"Loaded plugin: {plugin_config['name']} v{plugin_config.get('version', 'unknown')}")

            except KeyError as key_err:
                # T2-08: missing config key — warn and skip, do NOT crash the node.
                # Keeps minimal-template nodes bootable when a package-fallback plugin
                # needs a key the operator hasn't set.
                # Adversary #3 fix (v0.56.0): even on missing-config skip, security-critical
                # plugins marked `required = true` must still fail the startup rather than
                # silently disable (e.g., punchhole-frontend with C-02 default-deny,
                # firewall, etc.). Matches the required-flag check used by the other
                # skip branches above (handler_str missing, handler path escape,
                # general Exception).
                missing = str(key_err).strip("'\"")
                log.warning(
                    "PLUGIN_SKIPPED name=%s reason=missing_config key=%s",
                    plugin_config.get("name", plugin_path.name), missing,
                )
                if plugin_config.get("required", False):
                    failed_required.append(plugin_config.get("name", plugin_path.name))
            except Exception as e:
                log.warning(f"Failed to load handler for {plugin_path.name}: {e}")
                if plugin_config.get("required", False):
                    failed_required.append(plugin_config.get("name", plugin_path.name))
            finally:
                # V035-001: Remove bare-named modules loaded during this plugin
                for _n in _sibling_names:
                    sys.modules.pop(_n, None)
                sys.modules.update(_stashed_mods)
                # V015-009: Remove by value, not position
                try:
                    sys.path.remove(path_entry)
                except ValueError:
                    pass

        if failed_required:
            names = ", ".join(sorted(set(failed_required)))
            raise RuntimeError(f"Required plugin(s) failed to load: {names}")

    @staticmethod
    def _missing_required_config_keys(plugin_config: dict) -> list[str]:
        schema = plugin_config.get("config_schema", {})
        if not isinstance(schema, dict):
            return []
        required = schema.get("required", [])
        if not isinstance(required, list):
            return []
        config = plugin_config.get("config", {})
        if not isinstance(config, dict):
            config = {}
        missing = []
        for key in required:
            key_text = str(key or "").strip()
            if key_text and key_text not in config:
                missing.append(key_text)
        return missing

    async def dispatch_init(self, node: Any) -> None:
        """v0.57.0: run the plugin on_init lifecycle phase.

        PluginLoader constructs instances during ``load_plugins`` but never
        drove an init phase, so transport plugins that need to wire pool seams
        at startup (Tor) had nowhere to plug in. This method:

        1. Backfills ``ctx._node`` on every plugin context that was built via
           the legacy path (which sets ``_node=None``).  This gives plugins a
           handle onto the live DHTNode — they read ``node._pool``,
           ``node.node_info``, ``node._sidecar_port`` from it.
        2. Calls ``await plugin.on_init(ctx=plugin._ctx)`` on every plugin
           that defines ``on_init``. Failures are logged and swallowed; one
           plugin's init must not take down the node.

        Must be invoked AFTER the post-load context backfill in
        ``DHTNode.start`` (storage_path, sign_document, bus, group_engine)
        and BEFORE background loops start, so plugin-registered seams are in
        place when heartbeat/writer/pool loops begin using them.
        """
        for plugin in self.plugins:
            ctx = getattr(plugin, "_ctx", None)
            if ctx is not None and getattr(ctx, "_node", None) is None:
                ctx._node = node
        for plugin in self.plugins:
            on_init_fn = getattr(plugin, "on_init", None)
            if not callable(on_init_fn):
                continue
            try:
                await on_init_fn(ctx=getattr(plugin, "_ctx", None))
            except Exception as exc:
                log.warning(
                    "PLUGIN_ON_INIT_FAILED plugin=%s err=%s",
                    plugin.__class__.__name__, exc, exc_info=True,
                )

    async def on_connect(self, peer_ip: str) -> bool:
        for plugin in self.plugins:
            try:
                if not await plugin.on_connect(peer_ip):
                    return False
            except Exception as e:
                log.error(f"Plugin {plugin.__class__.__name__} failed in on_connect: {e}", exc_info=True)
                return False  # fail-closed
        return True

    async def on_inbound(self, msg: Message, peer_ip: str) -> bool:
        for plugin in self.plugins:
            try:
                if not await plugin.on_inbound(msg, peer_ip):
                    return False
            except Exception as e:
                log.error(f"Plugin {plugin.__class__.__name__} failed in on_inbound: {e}", exc_info=True)
                return False  # fail-closed
        return True

    async def on_outbound(self, msg: Message, peer: NodeInfo) -> bool:
        for plugin in self.plugins:
            try:
                if not await plugin.on_outbound(msg, peer):
                    return False
            except Exception as e:
                log.error(f"Plugin {plugin.__class__.__name__} failed in on_outbound: {e}", exc_info=True)
                return False  # fail-closed
        return True

    async def on_tick(self, peers: List[NodeInfo], health: NodeHealth) -> None:
        await asyncio.gather(*[
            self._safe_run_plugin_hook(plugin.on_tick, peers, health)
            for plugin in self.plugins
        ])

    async def on_query(self, query_type: str, value: str) -> List[Dict[str, Any]]:
        results = []
        for plugin in self.plugins:
            try:
                plugin_results = await plugin.on_query(query_type, value)
                results.extend(plugin_results)
            except Exception as e:
                log.error(f"Plugin {plugin.__class__.__name__} failed in on_query: {e}")
        return results

    async def on_task_complete(self, skill_name: str, job_id: str,
                              caller_node_id: str, result_data: Any,
                              wall_ms: int) -> None:
        await asyncio.gather(*[
            self._safe_run_plugin_hook(plugin.on_task_complete,
                                       skill_name, job_id, caller_node_id,
                                       result_data, wall_ms)
            for plugin in self.plugins
        ])

    async def on_mail_received(self, msg_type: str, from_node: str,
                               to_node: str, body: Any,
                               session_id: Optional[str]) -> None:
        await asyncio.gather(*[
            self._safe_run_plugin_hook(plugin.on_mail_received,
                                       msg_type, from_node, to_node,
                                       body, session_id)
            for plugin in self.plugins
        ])

    async def on_shutdown(self) -> None:
        await asyncio.gather(*[
            self._safe_run_plugin_hook(plugin.on_shutdown)
            for plugin in self.plugins
        ])

    # v0.36.0: Settlement hook delegation
    async def on_settlement_review(self, prepared_tx: dict) -> Optional[dict]:
        """First plugin that returns non-None wins. If all return None, settlement rejected."""
        for plugin in self.plugins:
            try:
                result = await plugin.on_settlement_review(prepared_tx)
                if result is not None:
                    return result
            except Exception as e:
                log.error(f"Plugin {plugin.__class__.__name__} failed in on_settlement_review: {e}")
                return None  # fail-closed
        return prepared_tx  # no plugin modified → auto-approve (hotwire)

    async def on_inbound_settlement(self, settle_request: dict) -> bool:
        """All plugins must agree. Any False → reject."""
        for plugin in self.plugins:
            try:
                if not await plugin.on_inbound_settlement(settle_request):
                    return False
            except Exception as e:
                log.error(f"Plugin {plugin.__class__.__name__} failed in on_inbound_settlement: {e}")
                return False  # fail-closed
        return True

    def get_plugin_by_name(self, name: str) -> Optional[PluginHooks]:
        """v0.46.0: Return the loaded plugin instance for the given plugin name, or None."""
        return self._name_to_plugin.get(name)

    async def _safe_run_plugin_hook(self, hook_method: Callable, *args, **kwargs):
        """Helper to run plugin hooks, catching exceptions to prevent crashing the node."""
        try:
            await hook_method(*args, **kwargs)
        except Exception as e:
            log.error(f"Plugin {hook_method.__self__.__class__.__name__} failed in {hook_method.__name__}: {e}", exc_info=True)
