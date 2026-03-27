"""E-01: Protocol message target_identity fields.

Tests:
1. TaskRequest has target_identity field defaulting to "".
2. MailPullReq has target_identity field defaulting to "".
3. PluginMessage has target_node_id field defaulting to "".
4. target_identity can be set on TaskRequest.
5. target_identity can be set on MailPullReq.
6. target_node_id can be set on PluginMessage.
7. Fields are preserved through serialization/deserialization.
"""
import pytest
import json
import dataclasses


class TestTargetIdentityFields:
    def test_task_request_has_target_identity(self):
        """TaskRequest has target_identity field with default ''."""
        from knarr.core.messages import TaskRequest
        req = TaskRequest()
        assert hasattr(req, "target_identity")
        assert req.target_identity == ""

    def test_mail_pull_req_has_target_identity(self):
        """MailPullReq has target_identity field with default ''."""
        from knarr.core.messages import MailPullReq
        req = MailPullReq()
        assert hasattr(req, "target_identity")
        assert req.target_identity == ""

    def test_plugin_message_has_target_node_id(self):
        """PluginMessage has target_node_id field with default ''."""
        from knarr.core.messages import PluginMessage
        msg = PluginMessage()
        assert hasattr(msg, "target_node_id")
        assert msg.target_node_id == ""

    def test_task_request_target_identity_set(self):
        """target_identity can be set on TaskRequest at construction."""
        from knarr.core.messages import TaskRequest
        node_id = "aa" * 32
        req = TaskRequest(target_identity=node_id)
        assert req.target_identity == node_id

    def test_mail_pull_req_target_identity_set(self):
        """target_identity can be set on MailPullReq at construction."""
        from knarr.core.messages import MailPullReq
        node_id = "bb" * 32
        req = MailPullReq(target_identity=node_id)
        assert req.target_identity == node_id

    def test_plugin_message_target_node_id_set(self):
        """target_node_id can be set on PluginMessage at construction."""
        from knarr.core.messages import PluginMessage
        node_id = "cc" * 32
        msg = PluginMessage(target_node_id=node_id)
        assert msg.target_node_id == node_id

    def test_task_request_serializes_target_identity(self):
        """target_identity is included in serialized form."""
        from knarr.core.messages import TaskRequest
        node_id = "dd" * 32
        req = TaskRequest(task_id="t1", target_identity=node_id)
        d = dataclasses.asdict(req)
        assert "target_identity" in d
        assert d["target_identity"] == node_id

    def test_plugin_message_serializes_target_node_id(self):
        """target_node_id is included in serialized form."""
        from knarr.core.messages import PluginMessage
        node_id = "ee" * 32
        msg = PluginMessage(plugin_name="test", target_node_id=node_id)
        d = dataclasses.asdict(msg)
        assert "target_node_id" in d
        assert d["target_node_id"] == node_id

    def test_existing_fields_unchanged(self):
        """Existing TaskRequest fields are not affected by new field."""
        from knarr.core.messages import TaskRequest
        req = TaskRequest(
            task_id="t1",
            skill_name="my_skill",
            requester_node_id="aa" * 32,
        )
        assert req.task_id == "t1"
        assert req.skill_name == "my_skill"
        assert req.requester_node_id == "aa" * 32
        assert req.target_identity == ""
