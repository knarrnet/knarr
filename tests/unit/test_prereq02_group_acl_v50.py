"""PREREQ-02: group:<name> ACL shorthand in punchhole backend.

BUG: Punchhole backend only supports tier-based ACL (all_signed/trusted/known_hosts/peer).
     Group-based access like `group:members` is not handled.

FIX: Add group:<name> shorthand. When required == "group:trade_guild", call
     GroupEngine.is_member(requester_node_id, "trade_guild"). Return True if member.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

_PLUGIN_PATH = Path(__file__).parents[2] / "src" / "knarr" / "plugins" / "09-punchhole-backend" / "handler.py"
_plugin_dir = str(_PLUGIN_PATH.parent)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
_punchhole_spec = importlib.util.spec_from_file_location("_punchhole09_handler", _PLUGIN_PATH)
_punchhole_mod = importlib.util.module_from_spec(_punchhole_spec)
_punchhole_spec.loader.exec_module(_punchhole_mod)
PunchholeBackendPlugin = _punchhole_mod.PunchholeBackendPlugin


def _make_ctx(group_engine=None, storage_path=None):
    ctx = MagicMock()
    ctx.node_id = "aa" * 32
    ctx.storage_path = storage_path
    ctx.plugin_dir = Path(__file__).parents[2] / "src" / "knarr" / "plugins" / "09-punchhole-backend"
    ctx.subscribe_events = None
    ctx.emit_event = None
    ctx.sign_document = None
    ctx.group_engine = group_engine
    return ctx


def _make_group_engine(members_by_group):
    """Return a simple GroupEngine-like object."""
    class FakeGroupEngine:
        def is_member(self, item_id, group):
            return item_id in members_by_group.get(group, set())
        def get_groups(self, item_id):
            return [g for g, m in members_by_group.items() if item_id in m]
    return FakeGroupEngine()


def test_group_acl_grants_member():
    """group:members ACL grants access to a node in the group."""
    ge = _make_group_engine({"members": {"node-alice"}})
    ctx = _make_ctx(group_engine=ge)
    plugin = PunchholeBackendPlugin(ctx, {})

    result = plugin._check_access("node-alice", "group:members")
    assert result is True, "PREREQ-02: member should be granted access"


def test_group_acl_denies_non_member():
    """group:members ACL denies a node NOT in the group."""
    ge = _make_group_engine({"members": {"node-alice"}})
    ctx = _make_ctx(group_engine=ge)
    plugin = PunchholeBackendPlugin(ctx, {})

    result = plugin._check_access("node-bob", "group:members")
    assert result is False, "PREREQ-02: non-member should be denied"


def test_group_acl_missing_group_engine_denies():
    """group:<name> ACL denies access if no GroupEngine is available."""
    ctx = _make_ctx(group_engine=None)
    plugin = PunchholeBackendPlugin(ctx, {})

    result = plugin._check_access("node-alice", "group:members")
    assert result is False, "PREREQ-02: no GroupEngine should deny access (fail closed)"


def test_group_acl_empty_group_denies():
    """group:<name> ACL denies if the group is empty."""
    ge = _make_group_engine({})  # no groups defined
    ctx = _make_ctx(group_engine=ge)
    plugin = PunchholeBackendPlugin(ctx, {})

    result = plugin._check_access("node-alice", "group:members")
    assert result is False, "PREREQ-02: empty group should deny"


def test_group_acl_different_group_denies():
    """group:<name> ACL denies node that is in a different group."""
    ge = _make_group_engine({"admins": {"node-alice"}, "members": set()})
    ctx = _make_ctx(group_engine=ge)
    plugin = PunchholeBackendPlugin(ctx, {})

    # alice is in admins, but we're checking group:members
    result = plugin._check_access("node-alice", "group:members")
    assert result is False, "PREREQ-02: membership in different group should not grant access"
