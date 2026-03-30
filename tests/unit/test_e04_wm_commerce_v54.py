"""E-04 tests: commerce URIs delegate to node._wm_ingest through the WM plugin."""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from knarr.core.messages import Envelope
from knarr.dht.identities import Identity, IdentityRegistry


def _load_wm_plugin_class():
    handler_path = Path("workspace/proposed-final/plugins/00-warehouse-membrane/handler.py")
    spec = importlib.util.spec_from_file_location("wm_handler_e04", handler_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.WarehouseMembranePlugin


def _make_registry(node_id: str) -> IdentityRegistry:
    registry = IdentityRegistry(default_node_id=node_id)
    registry.register(Identity(name="default", node_id=node_id))
    return registry


def test_commerce_uri_calls_node_wm_ingest():
    plugin_class = _load_wm_plugin_class()
    authority = "a" * 64
    wm_ingest = AsyncMock(return_value=SimpleNamespace(status="promoted"))
    ctx = SimpleNamespace(
        _node=SimpleNamespace(_identity_registry=_make_registry(authority), _wm_ingest=wm_ingest),
        _debug=False,
        get_plugin=lambda name: object() if name == "__never__" else None,
    )
    plugin = plugin_class(ctx, config={
        "rules": {"knarr://*/c/*": {"gates": [1, 2, 3, 4, 5], "action": "auto_promote"}},
    })

    async def _run():
        result = await plugin.on_inbound(
            Envelope(
                uri=f"knarr://{authority}/c/receipt/r1",
                payload=json.dumps({"document": {"document_type": "execution_receipt"}}),
                public_key="11" * 32,
            ),
            "127.0.0.1",
        )
        assert result is True

    asyncio.run(_run())
    assert wm_ingest.await_count == 1
    args = wm_ingest.await_args.args
    assert args[0]["document_type"] == "execution_receipt"
    assert args[1] == bytes.fromhex("11" * 32)


def test_commerce_uri_stops_when_wm_ingest_rejects():
    plugin_class = _load_wm_plugin_class()
    authority = "b" * 64
    wm_ingest = AsyncMock(return_value=SimpleNamespace(status="rejected", reason="bad_doc"))
    ctx = SimpleNamespace(
        _node=SimpleNamespace(_identity_registry=_make_registry(authority), _wm_ingest=wm_ingest),
        _debug=False,
        get_plugin=lambda name: object() if name == "__never__" else None,
    )
    plugin = plugin_class(ctx, config={
        "rules": {"knarr://*/c/*": {"gates": [1, 2, 3, 4, 5], "action": "auto_promote"}},
    })

    async def _run():
        result = await plugin.on_inbound(
            Envelope(
                uri=f"knarr://{authority}/c/receipt/r2",
                payload=json.dumps({"document": {"document_type": "execution_receipt"}}),
                public_key="22" * 32,
            ),
            "127.0.0.1",
        )
        assert result is False

    asyncio.run(_run())
    assert wm_ingest.await_count == 1
