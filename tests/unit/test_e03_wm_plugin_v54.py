"""E-03 tests: warehouse membrane plugin, priority ordering, and contextvars."""

import asyncio
import importlib.util
import inspect
import textwrap
import tomllib
from pathlib import Path
from types import SimpleNamespace

from knarr.core.messages import Envelope
from knarr.dht.identities import Identity, IdentityRegistry
from knarr.dht.node import _current_identity
from knarr.dht.plugins import PluginLoader


def _load_wm_plugin_class():
    handler_path = Path("workspace/proposed-final/plugins/00-warehouse-membrane/handler.py")
    spec = importlib.util.spec_from_file_location("wm_handler_test", handler_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.WarehouseMembranePlugin


def _make_registry(*node_ids: str) -> IdentityRegistry:
    registry = IdentityRegistry(default_node_id=node_ids[0] if node_ids else "")
    for index, node_id in enumerate(node_ids, 1):
        registry.register(Identity(name=f"ident-{index}", node_id=node_id))
    return registry


def _make_ctx(registry: IdentityRegistry, get_plugin=None):
    node = SimpleNamespace(_identity_registry=registry)
    return SimpleNamespace(
        _node=node,
        _debug=False,
        get_plugin=get_plugin or (lambda name: None),
    )


def _write_plugin(tmp_path: Path, dirname: str, name: str, priority: int, class_name: str) -> None:
    plugin_dir = tmp_path / "plugins" / dirname
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.toml").write_text(textwrap.dedent(f"""
        name = "{name}"
        version = "0.1.0"
        handler = "handler:{class_name}"
        priority = {priority}
    """).strip() + "\n")
    (plugin_dir / "handler.py").write_text(textwrap.dedent(f"""
        from knarr.dht.plugins import PluginHooks

        class {class_name}(PluginHooks):
            def __init__(self, ctx, config):
                self._ctx = ctx
    """).strip() + "\n")


def test_wm_plugin_toml_has_priority_zero_and_is_required():
    data = tomllib.loads(Path("workspace/proposed-final/plugins/00-warehouse-membrane/plugin.toml").read_text())
    assert data["priority"] == 0
    assert data["required"] is True


def test_plugin_loader_sorts_by_priority_then_name(tmp_path):
    _write_plugin(tmp_path, "20-zeta", "zeta", 20, "ZetaPlugin")
    _write_plugin(tmp_path, "10-beta", "beta", 10, "BetaPlugin")
    _write_plugin(tmp_path, "10-alpha", "alpha", 10, "AlphaPlugin")

    loader = PluginLoader(
        config_dir=tmp_path,
        get_peers_cb=lambda: [],
        send_to_peer_cb=lambda *args, **kwargs: None,
        node_id="a" * 64,
    )
    loader.load_plugins()
    assert [type(plugin).__name__ for plugin in loader.plugins] == [
        "AlphaPlugin",
        "BetaPlugin",
        "ZetaPlugin",
    ]


def test_required_plugin_failure_aborts_startup(tmp_path):
    plugin_dir = tmp_path / "plugins" / "00-required"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.toml").write_text(textwrap.dedent("""
        name = "required-broken"
        version = "0.1.0"
        handler = "missing:BrokenPlugin"
        priority = 0
        required = true
    """).strip() + "\n")

    loader = PluginLoader(
        config_dir=tmp_path,
        get_peers_cb=lambda: [],
        send_to_peer_cb=lambda *args, **kwargs: None,
        node_id="a" * 64,
    )
    try:
        loader.load_plugins()
    except RuntimeError as exc:
        assert "required-broken" in str(exc)
    else:
        raise AssertionError("required plugin failure should abort startup")


def test_wm_gate_pipeline_rejects_unknown_identity():
    plugin_class = _load_wm_plugin_class()
    plugin = plugin_class(_make_ctx(_make_registry()), config={
        "rules": {"knarr://*/k/*": {"gates": [1, 2], "action": "auto_promote"}},
    })
    msg = Envelope(uri=f"knarr://{'b' * 64}/k/ping", payload="{}")
    assert asyncio.run(plugin.on_inbound(msg, "127.0.0.1")) is False


def test_wm_selector_registration_and_uri_pattern_rules():
    plugin_class = _load_wm_plugin_class()
    authority = "a" * 64

    plugin_missing = plugin_class(_make_ctx(_make_registry(authority)), config={
        "rules": {
            "knarr://*/p/*": {"gates": [1, 2, 3], "action": "auto_promote"},
            "knarr://*/c/*": {"gates": [1, 2, 3, 4, 5], "action": "auto_promote"},
            "knarr://*/k/*": {"gates": [1, 2], "action": "auto_promote"},
        },
    })
    p_msg = Envelope(uri=f"knarr://{authority}/p/object", payload="{}")
    assert asyncio.run(plugin_missing.on_inbound(p_msg, "127.0.0.1")) is False

    plugin_present = plugin_class(_make_ctx(
        _make_registry(authority),
        get_plugin=lambda name: object() if name == "knarr-punchhole" else None,
    ), config={
        "rules": {
            "knarr://*/p/*": {"gates": [1, 2, 3], "action": "auto_promote"},
            "knarr://*/c/*": {"gates": [1, 2, 3, 4, 5], "action": "auto_promote"},
            "knarr://*/k/*": {"gates": [1, 2], "action": "auto_promote"},
        },
    })
    assert asyncio.run(plugin_present.on_inbound(p_msg, "127.0.0.1")) is True

    k_msg = Envelope(uri=f"knarr://{authority}/k/ping", payload="{}")
    c_msg = Envelope(uri=f"knarr://{authority}/c/receipt/r1", payload="{}")
    assert asyncio.run(plugin_present.on_inbound(k_msg, "127.0.0.1")) is True
    assert asyncio.run(plugin_present.on_inbound(c_msg, "127.0.0.1")) is False


def test_contextvars_isolate_identity_scope_across_tasks():
    plugin_class = _load_wm_plugin_class()
    node_a = "a" * 64
    node_b = "b" * 64
    plugin = plugin_class(_make_ctx(_make_registry(node_a, node_b)), config={
        "rules": {"knarr://*/k/*": {"gates": [1, 2], "action": "auto_promote"}},
    })

    async def _run_one(node_id: str) -> str:
        _current_identity.set(None)
        msg = Envelope(uri=f"knarr://{node_id}/k/ping", payload="{}")
        ok = await plugin.on_inbound(msg, "127.0.0.1")
        assert ok is True
        await asyncio.sleep(0.01)
        current = _current_identity.get()
        result = current.node_id if current is not None else ""
        _current_identity.set(None)
        return result

    async def _run():
        return await asyncio.gather(_run_one(node_a), _run_one(node_b))

    results = asyncio.run(_run())
    assert results == [node_a, node_b]


def test_node_no_longer_uses_swap_and_restore_for_identity_scope():
    from knarr.dht.node import DHTNode

    source = inspect.getsource(DHTNode)
    assert "_orig_storage = self.storage" not in source
