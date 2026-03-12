import asyncio
import dataclasses
import importlib.util
import logging
import sys
import tomllib
from pathlib import Path
from typing import Callable, Dict, List, Optional, Type, Any

from knarr.core.messages import Message
from knarr.core.models import NodeInfo

log = logging.getLogger(__name__)


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


@dataclasses.dataclass
class PluginContext:
    """Provides plugins with access to node information and utilities."""
    node_id: str
    plugin_dir: Path
    get_peers: Callable[[], List[NodeInfo]]
    send_to_peer: Callable[[NodeInfo, Message], Any]
    send_fire_forget: Callable[[NodeInfo, Message], Any]
    delivery_cb: Optional[Callable[[Message, str], Any]]
    log: logging.Logger
    state_dir: Optional[Path] = None
    group_engine: Optional[Any] = None      # GroupEngine instance, set after engine init
    storage_path: Optional[str] = None      # Path to node.db for read-only queries
    register_mail_handler: Optional[Callable] = None
    send_mail: Optional[Callable] = None
    register_egress_material: Optional[Callable] = None
    vault_get: Optional[Callable] = None
    vault_set: Optional[Callable] = None
    update_cache: Optional[Callable] = None  # ctx.update_cache(realm, key, data_dict)
    subscribe_events: Optional[Callable] = None   # v0.32.0: (*patterns) -> Subscriber
    emit_event: Optional[Callable] = None         # v0.32.0: (event_type, **fields) -> None
    bus: Optional[Any] = None                     # v0.33.0: EventBus reference
    sign_document: Optional[Callable] = None      # v0.35.0: sign dict per eddsa-jcs-2022
    query_receipts: Optional[Callable] = None     # v0.35.0: query receipt_log with filters
    query_prepaid_balance: Optional[Callable] = None  # v0.36.0: (peer_key) -> float
    economy_config: Optional[dict] = None         # v0.42.0: global [economy] config section


class PluginLoader:
    """
    Discovers, loads, and manages Knarr plugins from the specified plugin directories.
    """
    def __init__(self, config_dir: Path, get_peers_cb: Callable, send_to_peer_cb: Callable, node_id: str, delivery_cb: Optional[Callable] = None, send_fire_forget_cb: Optional[Callable] = None, register_mail_handler_cb: Optional[Callable] = None, send_mail_cb: Optional[Callable] = None, register_egress_material_cb: Optional[Callable] = None, vault_get_cb: Optional[Callable] = None, vault_set_cb: Optional[Callable] = None, storage_path: Optional[str] = None, update_cache_cb: Optional[Callable] = None, subscribe_events_cb: Optional[Callable] = None, emit_event_cb: Optional[Callable] = None, bus: Optional[Any] = None, data_dir: Optional[Path] = None):
        self._plugin_root = config_dir / "plugins"
        self._state_root = data_dir / "plugin_state" if data_dir else None
        self._get_peers_cb = get_peers_cb
        self._send_to_peer_cb = send_to_peer_cb
        self._send_fire_forget_cb = send_fire_forget_cb
        self._register_mail_handler_cb = register_mail_handler_cb
        self._send_mail_cb = send_mail_cb
        self._register_egress_material_cb = register_egress_material_cb
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

    def load_plugins(self) -> None:
        """
        Scans for and loads plugins. Logs warnings for failures but continues startup.
        """
        if not self._plugin_root.is_dir():
            log.info(f"Plugin directory not found: {self._plugin_root}. No plugins loaded.")
            return

        for plugin_path in sorted(self._plugin_root.iterdir()):
            if not plugin_path.is_dir():
                continue

            toml_path = plugin_path / "plugin.toml"
            if not toml_path.is_file():
                log.warning(f"Skipping {plugin_path.name}: plugin.toml not found.")
                continue

            try:
                plugin_config = tomllib.loads(toml_path.read_text())
                handler_str = plugin_config.get("handler")
                if not handler_str:
                    log.warning(f"Skipping {plugin_path.name}: 'handler' not specified in plugin.toml.")
                    continue

                module_name, class_name = handler_str.split(":")

                # V015-008: Confine handler path to plugin directory
                handler_file = (plugin_path / f"{module_name}.py").resolve()
                if not handler_file.is_relative_to(plugin_path.resolve()):
                    log.warning(f"Skipping {plugin_path.name}: handler path escapes plugin directory.")
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
                        state_dir=state_dir,
                        get_peers=self._get_peers_cb,
                        send_to_peer=self._send_to_peer_cb,
                        send_fire_forget=self._send_fire_forget_cb or self._send_to_peer_cb,
                        delivery_cb=self._delivery_cb,
                        log=plugin_logger,
                        register_mail_handler=self._register_mail_handler_cb,
                        send_mail=self._send_mail_cb,
                        register_egress_material=self._register_egress_material_cb,
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
                    log.info(f"Loaded plugin: {plugin_config['name']} v{plugin_config.get('version', 'unknown')}")

                except (ImportError, AttributeError, TypeError) as e:
                    log.warning(f"Failed to load handler for {plugin_path.name}: {e}")
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

            except Exception as e:
                log.warning(f"Failed to parse plugin.toml for {plugin_path.name}: {e}")

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

    async def _safe_run_plugin_hook(self, hook_method: Callable, *args, **kwargs):
        """Helper to run plugin hooks, catching exceptions to prevent crashing the node."""
        try:
            await hook_method(*args, **kwargs)
        except Exception as e:
            log.error(f"Plugin {hook_method.__self__.__class__.__name__} failed in {hook_method.__name__}: {e}", exc_info=True)
