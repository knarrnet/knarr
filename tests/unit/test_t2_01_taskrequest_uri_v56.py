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


def test_validate_task_request_uri_accepts_self_target_happy_path():
    """v0.56.1 regression test — catches the Wave 1 gap that shipped.

    ``submit_async_task`` at ``node.py:1769`` + ``:3043`` unconditionally sets
    ``target_identity=provider_node_id`` (E-01 convention since v0.55.0). On the
    receiver side, the receiver IS the provider, so ``target_identity ==
    self.node_info.node_id`` for every legitimate cross-node TaskRequest.

    Before the v0.56.1 hotfix: the validator called ``_identity_registry.resolve(
    target_identity)``, which returned None on every single-identity node (the
    registry's ``_identities`` dict is empty by default — nodes don't auto-register
    their own Identity). ``reason = "authority_unknown_identity"`` → every
    TaskRequest rejected → cluster gate caught the regression on Viggo's v0.56.0
    upgrade (G-18 working as designed).

    After the hotfix: ``target_identity == self.node_info.node_id`` takes the
    self-target fast path and accepts when URI authority also matches self.

    This test would have caught the bug at Wave 1 time if it had existed. Adding
    it now closes the happy-path test gap for v0.56.1 and forward.
    """
    node = _make_node()
    node_id = "a" * 64  # fixture's node_info.node_id
    msg = TaskRequest(
        task_id="task-happy",
        requester_node_id="r" * 64,
        requester_host="127.0.0.1",
        requester_port=9001,
        skill_name="echo",
        target_identity=node_id,  # self-target (E-01 convention)
        uri="knarr://" + node_id + "/s/echo",  # matching authority
    )
    response = node._validate_task_request_uri(msg, "echo")
    # Happy path returns None (no rejection)
    assert response is None, (
        f"Self-target TaskRequest should pass validation, got rejection: "
        f"{getattr(response, 'error', None)}"
    )
    # Bus should NOT emit security.uri_mismatch on the happy path
    mismatch_calls = [
        call for call in node.bus.emit.call_args_list
        if call.args and call.args[0] == "security.uri_mismatch"
    ]
    assert not mismatch_calls, f"Unexpected uri_mismatch emits: {mismatch_calls}"


def test_validate_task_request_uri_rejects_foreign_unresolvable_target_identity():
    """Adversary #8 fix regression guard — foreign target_identity still rejects.

    When target_identity is set to a node_id that is NOT this node's own AND not
    in the IdentityRegistry, the validator must reject with
    ``authority_unknown_identity``. This preserves the GPT #8 adversary fix
    (foreign target_identity must not silently fall through to default identity
    execution).

    Distinct from the self-target fast path test above: this uses a target_identity
    that does not match self.node_info.node_id.
    """
    node = _make_node()  # node_info.node_id = "a" * 64
    foreign = "f" * 64
    msg = TaskRequest(
        task_id="task-foreign",
        requester_node_id="r" * 64,
        requester_host="127.0.0.1",
        requester_port=9001,
        skill_name="echo",
        target_identity=foreign,
        uri="knarr://" + foreign + "/s/echo",
    )
    response = node._validate_task_request_uri(msg, "echo")
    assert isinstance(response, TaskResult)
    assert response.status == "failed"
    assert response.error["code"] == "URI_MISMATCH"
    assert node.bus.emit.call_args.kwargs["reason"] == "authority_unknown_identity"


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
