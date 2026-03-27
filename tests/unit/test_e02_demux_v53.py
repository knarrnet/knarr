"""E-02: Connection handler identity demux.

Tests _resolve_target_identity(msg) on DHTNode:
1. TaskRequest with target_identity set returns that identity.
2. MailPullReq with target_identity set returns that identity.
3. PluginMessage with target_node_id set returns that node_id.
4. MailSync with items[0].to_node returns that to_node.
5. TaskRequest with skill_name in _skill_to_identity map returns mapped identity.
6. Message with no routing info returns None (default identity).
7. TaskRequest with empty target_identity falls through to skill map.
8. _resolve_target_identity method exists on DHTNode.
"""
import pytest
from unittest.mock import MagicMock


def _make_node():
    """Build a minimal DHTNode with mocked infrastructure."""
    from knarr.dht.node import DHTNode
    node = DHTNode.__new__(DHTNode)
    node._skill_to_identity = {}
    node._debug = False
    return node


class TestResolveDemux:
    def test_method_exists(self):
        """_resolve_target_identity method exists on DHTNode."""
        from knarr.dht.node import DHTNode
        assert hasattr(DHTNode, "_resolve_target_identity")

    def test_task_request_target_identity(self):
        """TaskRequest with target_identity returns that identity."""
        from knarr.core.messages import TaskRequest
        node = _make_node()
        nid = "aa" * 32
        msg = TaskRequest(target_identity=nid, skill_name="test")
        result = node._resolve_target_identity(msg)
        assert result == nid

    def test_mail_pull_req_target_identity(self):
        """MailPullReq with target_identity returns that identity."""
        from knarr.core.messages import MailPullReq
        node = _make_node()
        nid = "bb" * 32
        msg = MailPullReq(target_identity=nid)
        result = node._resolve_target_identity(msg)
        assert result == nid

    def test_plugin_message_target_node_id(self):
        """PluginMessage with target_node_id returns that node_id."""
        from knarr.core.messages import PluginMessage
        node = _make_node()
        nid = "cc" * 32
        msg = PluginMessage(target_node_id=nid, plugin_name="test")
        result = node._resolve_target_identity(msg)
        assert result == nid

    def test_mail_sync_routes_by_to_node(self):
        """MailSync with items[0].to_node returns to_node."""
        from knarr.core.messages import MailSync
        node = _make_node()
        nid = "dd" * 32
        msg = MailSync(items=[{"to_node": nid, "msg_id": "m1"}])
        result = node._resolve_target_identity(msg)
        assert result == nid

    def test_task_request_skill_map_routing(self):
        """TaskRequest with skill_name in _skill_to_identity map uses that identity."""
        from knarr.core.messages import TaskRequest
        node = _make_node()
        nid = "ee" * 32
        node._skill_to_identity["llm/chat@1.0"] = nid
        msg = TaskRequest(skill_name="llm/chat@1.0")
        result = node._resolve_target_identity(msg)
        assert result == nid

    def test_no_routing_returns_none(self):
        """Message with no routing info returns None (use default)."""
        from knarr.core.messages import TaskRequest
        node = _make_node()
        msg = TaskRequest(skill_name="unknown_skill")
        result = node._resolve_target_identity(msg)
        assert result is None

    def test_empty_target_identity_falls_through_to_skill_map(self):
        """TaskRequest with empty target_identity falls through to skill map."""
        from knarr.core.messages import TaskRequest
        node = _make_node()
        nid = "ff" * 32
        node._skill_to_identity["my_skill"] = nid
        msg = TaskRequest(skill_name="my_skill", target_identity="")
        result = node._resolve_target_identity(msg)
        assert result == nid

    def test_mail_sync_empty_items_returns_none(self):
        """MailSync with empty items returns None."""
        from knarr.core.messages import MailSync
        node = _make_node()
        msg = MailSync(items=[])
        result = node._resolve_target_identity(msg)
        assert result is None
