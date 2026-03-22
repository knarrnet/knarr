"""BUG-04: skill.registered and skill.removed bus events must be emitted.

BUG: punchhole backend subscribes to `skill.registered` and `skill.removed` for
cache invalidation, but no code emits these events. Cache is never invalidated.

FIX: announce() emits skill.registered; deregister() emits skill.removed.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


def _make_node_with_bus():
    """Create a minimal DHTNode stub with a mocked bus."""
    from knarr.dht.node import DHTNode
    from knarr.core.models import NodeInfo

    node = MagicMock(spec=DHTNode)
    node._version_gated = False
    node._own_skills = {}
    node._skill_visibility = {}
    node._gossip_fanout = 3
    node._sidecar_port = 9001
    node._encryption_key_hex = ""
    node._wallet = ""
    node._debug = False
    node._public_key_hex = "aa" * 32
    node._node_jurisdiction_wire = ""
    node.node_info = NodeInfo(node_id="aa" * 32, host="127.0.0.1", port=9000)

    # Mock bus with emit tracking
    bus = MagicMock()
    bus.emit = MagicMock()
    node.bus = bus

    storage = MagicMock()
    storage.get_peers.return_value = []
    node.storage = storage

    node._enqueue_write = AsyncMock()
    node._update_meta_cache = MagicMock()
    node.refresh_node_meta = MagicMock()

    msg_mock = MagicMock()
    msg_mock.public_key = "aa" * 32
    msg_mock.signature = "sig"
    msg_mock.msg_id = "mid"
    msg_mock.sidecar_port = 9001
    node._sign = lambda m: msg_mock

    return node


@pytest.mark.asyncio
async def test_skill_registered_event_emitted_on_announce():
    """announce() must emit skill.registered bus event."""
    from knarr.dht.node import DHTNode

    node = _make_node_with_bus()

    skill_data = {
        "name": "test-skill",
        "version": "1.0.0",
        "description": "test skill",
        "tags": ["test"],
        "input_schema": {},
        "output_schema": {},
        "price": 1.0,
        "max_input_size": 1024,
    }

    with patch("asyncio.create_task"):
        await DHTNode.announce(node, skill_data)

    # Verify skill.registered was emitted
    emitted_events = [call[0][0] for call in node.bus.emit.call_args_list]
    assert "skill.registered" in emitted_events, (
        f"skill.registered not emitted. Got: {emitted_events}. "
        "BUG-04: announce() must emit skill.registered for punchhole cache invalidation."
    )

    # Verify the event includes skill name and identity
    skill_reg_calls = [
        call for call in node.bus.emit.call_args_list
        if call[0][0] == "skill.registered"
    ]
    assert len(skill_reg_calls) == 1
    _, kwargs = skill_reg_calls[0]
    assert kwargs.get("skill") == "test-skill", f"skill field missing or wrong: {kwargs}"
    assert "identity" in kwargs, f"identity field missing: {kwargs}"


@pytest.mark.asyncio
async def test_skill_removed_event_emitted_on_deregister():
    """deregister() must emit skill.removed bus event."""
    from knarr.dht.node import DHTNode
    from knarr.core.models import SkillSheet

    node = _make_node_with_bus()

    # Pre-register the skill
    skill = MagicMock(spec=SkillSheet)
    skill.name = "test-skill"
    node._own_skills["test-skill"] = skill
    node.refresh_node_meta = MagicMock()

    with patch("asyncio.create_task"):
        await DHTNode.deregister(node, "test-skill")

    emitted_events = [call[0][0] for call in node.bus.emit.call_args_list]
    assert "skill.removed" in emitted_events, (
        f"skill.removed not emitted. Got: {emitted_events}. "
        "BUG-04: deregister() must emit skill.removed for punchhole cache invalidation."
    )

    # Verify the event includes skill name and identity
    skill_rem_calls = [
        call for call in node.bus.emit.call_args_list
        if call[0][0] == "skill.removed"
    ]
    assert len(skill_rem_calls) == 1
    _, kwargs = skill_rem_calls[0]
    assert kwargs.get("skill") == "test-skill", f"skill field missing or wrong: {kwargs}"
    assert "identity" in kwargs, f"identity field missing: {kwargs}"


@pytest.mark.asyncio
async def test_no_skill_removed_event_for_nonexistent_skill():
    """deregister() must NOT emit skill.removed if skill not owned."""
    from knarr.dht.node import DHTNode

    node = _make_node_with_bus()
    node._own_skills = {}  # empty — skill not registered

    with patch("asyncio.create_task"):
        await DHTNode.deregister(node, "nonexistent-skill")

    emitted_events = [call[0][0] for call in node.bus.emit.call_args_list]
    assert "skill.removed" not in emitted_events, (
        "skill.removed must not be emitted for a skill that wasn't registered."
    )


@pytest.mark.asyncio
async def test_skill_registered_event_not_emitted_when_no_bus():
    """announce() must not crash if bus is None."""
    from knarr.dht.node import DHTNode

    node = _make_node_with_bus()
    node.bus = None  # no bus

    skill_data = {
        "name": "test-skill",
        "version": "1.0.0",
        "description": "test skill",
        "tags": ["test"],
        "input_schema": {},
        "output_schema": {},
        "price": 1.0,
        "max_input_size": 1024,
    }

    with patch("asyncio.create_task"):
        result = await DHTNode.announce(node, skill_data)

    assert result == "test-skill", f"announce() failed: {result}"
