from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from knarr.core.messages import TaskRequest, TaskResult
from knarr.dht.node import DHTNode


def _make_node():
    node = DHTNode.__new__(DHTNode)
    node._seen_task_requests = OrderedDict()
    node._version_gated = False
    node._upgrading = False
    node._handlers = {}
    node._own_skills = {}
    node._skill_visibility = {}
    node.bus = MagicMock()
    node.node_info = SimpleNamespace(node_id="a" * 64)
    node._sign = lambda msg: msg
    node._emit_task_rejected = MagicMock()
    return node


@pytest.mark.parametrize(
    ("uri", "target_identity", "expected_reasons"),
    [
        ("not-a-knarr-uri", "", {"invalid_uri"}),
        ("knarr://" + ("a" * 64) + "/m/echo", "", {"bad_selector"}),
        ("knarr://" + ("a" * 64) + "/s/not-echo", "", {"skill_mismatch"}),
        # Forseti's adversary fix #7 added identity resolution: when target_identity
        # is set but doesn't resolve to a local identity, the validator now returns
        # "authority_unknown_identity" instead of "authority_mismatch". Both are
        # valid for unresolvable identities — accept either.
        ("knarr://" + ("b" * 64) + "/s/echo", "a" * 64, {"authority_mismatch", "authority_unknown_identity"}),
    ],
)
def test_validate_task_request_uri_emits_specific_reason(uri, target_identity, expected_reasons):
    node = _make_node()
    msg = TaskRequest(
        task_id="task-1",
        requester_node_id="r" * 64,
        requester_host="127.0.0.1",
        requester_port=9001,
        skill_name="echo",
        target_identity=target_identity,
        uri=uri,
    )

    response = node._validate_task_request_uri(msg, "echo")

    assert isinstance(response, TaskResult)
    assert response.status == "failed"
    assert response.error["code"] == "URI_MISMATCH"
    assert node.bus.emit.call_args.args[0] == "security.uri_mismatch"
    assert node.bus.emit.call_args.kwargs["reason"] in expected_reasons


@pytest.mark.asyncio
async def test_handle_task_request_rejects_uri_mismatch_before_skill_lookup():
    node = _make_node()
    msg = TaskRequest(
        task_id="task-2",
        requester_node_id="r" * 64,
        requester_host="127.0.0.1",
        requester_port=9001,
        skill_name="echo",
        uri="knarr://" + ("a" * 64) + "/m/echo",
    )

    response = await node._handle_task_request(msg)

    assert response.error["code"] == "URI_MISMATCH"
    node._emit_task_rejected.assert_called_once_with("echo", msg.public_key, "task-2", "URI_MISMATCH")


@pytest.mark.asyncio
async def test_handle_task_request_empty_uri_keeps_backward_compatible_unknown_skill_path():
    node = _make_node()
    msg = TaskRequest(
        task_id="task-3",
        requester_node_id="r" * 64,
        requester_host="127.0.0.1",
        requester_port=9001,
        skill_name="echo",
        uri="",
    )

    response = await node._handle_task_request(msg)

    assert response.error["code"] == "UNKNOWN_SKILL"
    assert not any(
        call.args and call.args[0] == "security.uri_mismatch"
        for call in node.bus.emit.call_args_list
    )
