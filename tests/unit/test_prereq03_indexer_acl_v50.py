"""PREREQ-03: indexer:<name> ACL shorthand in punchhole backend.

BUG: Punchhole backend has no concept of named indexer ACL entries.
     `indexer:verein_indexer` is not a recognized access control form.

FIX: Add indexer:<name> shorthand. When required == "indexer:verein_indexer",
     look up "verein_indexer" in config indexers dict and check if the
     requester_node_id matches the registered node_id.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

_PLUGIN_PATH = Path(__file__).parents[2] / "src" / "knarr" / "plugins" / "09-punchhole-backend" / "handler.py"
_plugin_dir = str(_PLUGIN_PATH.parent)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
_punchhole_spec = importlib.util.spec_from_file_location("_punchhole09b_handler", _PLUGIN_PATH)
_punchhole_mod = importlib.util.module_from_spec(_punchhole_spec)
_punchhole_spec.loader.exec_module(_punchhole_mod)
PunchholeBackendPlugin = _punchhole_mod.PunchholeBackendPlugin


def _make_ctx(storage_path=None):
    ctx = MagicMock()
    ctx.node_id = "aa" * 32
    ctx.storage_path = storage_path
    ctx.plugin_dir = Path(__file__).parents[2] / "src" / "knarr" / "plugins" / "09-punchhole-backend"
    ctx.subscribe_events = None
    ctx.emit_event = None
    ctx.sign_document = None
    ctx.group_engine = None
    return ctx


_INDEXER_NODE_ID = "bb" * 32
_OTHER_NODE_ID   = "cc" * 32


def test_indexer_acl_grants_registered_node():
    """indexer:verein_indexer ACL grants access to the registered node_id."""
    config = {"indexers": {"verein_indexer": _INDEXER_NODE_ID}}
    ctx = _make_ctx()
    plugin = PunchholeBackendPlugin(ctx, config)

    result = plugin._check_access(_INDEXER_NODE_ID, "indexer:verein_indexer")
    assert result is True, "PREREQ-03: registered indexer node_id should be granted access"


def test_indexer_acl_denies_other_node():
    """indexer:verein_indexer ACL denies a node_id that is not the registered indexer."""
    config = {"indexers": {"verein_indexer": _INDEXER_NODE_ID}}
    ctx = _make_ctx()
    plugin = PunchholeBackendPlugin(ctx, config)

    result = plugin._check_access(_OTHER_NODE_ID, "indexer:verein_indexer")
    assert result is False, "PREREQ-03: non-registered node_id should be denied"


def test_indexer_acl_unknown_name_denies():
    """indexer:<name> ACL denies if the indexer name is not registered."""
    config = {"indexers": {"verein_indexer": _INDEXER_NODE_ID}}
    ctx = _make_ctx()
    plugin = PunchholeBackendPlugin(ctx, config)

    result = plugin._check_access(_INDEXER_NODE_ID, "indexer:unknown_indexer")
    assert result is False, "PREREQ-03: unregistered indexer name should deny"


def test_indexer_acl_no_config_denies():
    """indexer:<name> ACL denies if no indexers are configured."""
    ctx = _make_ctx()
    plugin = PunchholeBackendPlugin(ctx, {})  # no indexers config

    result = plugin._check_access(_INDEXER_NODE_ID, "indexer:verein_indexer")
    assert result is False, "PREREQ-03: no indexers config should deny (fail closed)"


def test_indexer_acl_does_not_affect_tier_check():
    """Non-indexer: access entry still uses normal tier logic."""
    config = {"indexers": {"verein_indexer": _INDEXER_NODE_ID}}
    ctx = _make_ctx()
    plugin = PunchholeBackendPlugin(ctx, config)

    # "all_signed" tier is the least privileged — any valid node passes
    # _resolve_acl_group returns "all_signed" when no storage is present
    result = plugin._check_access(_INDEXER_NODE_ID, "all_signed")
    assert result is True, "PREREQ-03: standard tier check must still work alongside indexer shorthand"
