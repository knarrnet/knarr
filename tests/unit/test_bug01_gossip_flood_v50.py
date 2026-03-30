"""BUG-01 (updated): _reannounce_all() uses sqrt(N) fanout with fire-and-forget.

Original BUG: scheduled 300s republish used random.sample(peers, fanout=3), causing
sparse coverage at scale (100-node: median node knows only 44/100 providers).

FIX (v0.50+): _reannounce_all() uses sqrt(N) fanout with fire-and-forget (no pool
lock) and respects on_outbound plugin hook. announce() hot-path still uses fanout=3.
"""
import asyncio
import math
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import types


def _bind_reannounce(node):
    """Bind _reannounce_all from DHTNode to the node mock."""
    from knarr.dht.node import DHTNode
    node._reannounce_all = types.MethodType(DHTNode._reannounce_all, node)
    return node


def _make_node_stub(peer_count: int = 10):
    """Create a minimal node stub for testing _reannounce_all."""
    from knarr.core.models import NodeInfo

    node = MagicMock()
    node._version_gated = False
    node._gossip_fanout = 3
    node._sidecar_port = 9001
    node._encryption_key_hex = ""
    node._wallet = ""
    # _node_jurisdiction_wire is a property in the real class; use a plain attribute here
    type(node)._node_jurisdiction_wire = property(lambda self: "")
    node._debug = False
    node.node_info = NodeInfo(node_id="aa" * 32, host="127.0.0.1", port=9000)

    # Build fake peers
    peers = [
        NodeInfo(node_id=f"{i:02x}" * 32, host=f"10.0.0.{i+1}", port=9000 + i)
        for i in range(peer_count)
    ]
    node.storage = MagicMock()
    node.storage.get_peers.return_value = peers

    msg_mock = MagicMock()
    node._sign = lambda m: msg_mock

    # Plugin hook: allow all outbound by default
    node._plugins = MagicMock()
    node._plugins.on_outbound = AsyncMock(return_value=True)

    # Fire-and-forget sender
    node._send_fire_forget = AsyncMock()

    _bind_reannounce(node)
    return node, peers


@pytest.mark.asyncio
async def test_reannounce_sqrt_fanout():
    """_reannounce_all() sends to sqrt(N) peers, not all peers."""
    from knarr.core.models import SkillSheet

    node, peers = _make_node_stub(peer_count=100)

    skill = MagicMock(spec=SkillSheet)
    skill.name = "test-skill"
    skill.to_dict.return_value = {"name": "test-skill"}
    node._own_skills = {"test-skill": skill}
    node._skill_visibility = {"test-skill": "public"}

    created_tasks = []
    with patch("asyncio.create_task", side_effect=lambda coro: (created_tasks.append(coro), asyncio.get_event_loop().create_task(coro))[1]):
        await node._reannounce_all()
        await asyncio.sleep(0)

    expected_fanout = max(3, int(math.sqrt(100)))  # 10
    assert len(created_tasks) == expected_fanout, (
        f"_reannounce_all() created {len(created_tasks)} tasks, expected {expected_fanout} "
        f"(sqrt({100}) fanout). Must NOT send to all 100 peers."
    )


@pytest.mark.asyncio
async def test_reannounce_max_fanout_cap():
    """_reannounce_all() with small peer count uses min(max_fanout, len(peers))."""
    from knarr.core.models import SkillSheet

    node, peers = _make_node_stub(peer_count=4)

    skill = MagicMock(spec=SkillSheet)
    skill.name = "test-skill"
    skill.to_dict.return_value = {"name": "test-skill"}
    node._own_skills = {"test-skill": skill}
    node._skill_visibility = {"test-skill": "public"}

    created_tasks = []
    with patch("asyncio.create_task", side_effect=lambda coro: (created_tasks.append(coro), asyncio.get_event_loop().create_task(coro))[1]):
        await node._reannounce_all()
        await asyncio.sleep(0)

    # max_fanout = max(3, int(sqrt(4))) = 3; min(3, 4) = 3
    expected = min(max(3, int(math.sqrt(4))), 4)
    assert len(created_tasks) == expected, (
        f"_reannounce_all() created {len(created_tasks)} tasks, expected {expected}. "
        f"fanout=max(3,sqrt(N)) capped at peer count."
    )


@pytest.mark.asyncio
async def test_reannounce_skips_private_skills():
    """_reannounce_all() must skip private skills."""
    from knarr.core.models import SkillSheet

    node, peers = _make_node_stub(peer_count=5)

    skill_pub = MagicMock(spec=SkillSheet)
    skill_pub.name = "public-skill"
    skill_pub.to_dict.return_value = {"name": "public-skill"}

    skill_priv = MagicMock(spec=SkillSheet)
    skill_priv.name = "private-skill"
    skill_priv.to_dict.return_value = {"name": "private-skill"}

    node._own_skills = {
        "public-skill": skill_pub,
        "private-skill": skill_priv,
    }
    node._skill_visibility = {
        "public-skill": "public",
        "private-skill": "private",
    }

    created_tasks = []

    with patch("asyncio.create_task", side_effect=lambda coro: (created_tasks.append(coro), asyncio.get_event_loop().create_task(coro))[1]):
        await node._reannounce_all()
        await asyncio.sleep(0)

    # sqrt(5) = 2.2 -> max(3, 2) = 3; min(3, 5) = 3 targets for 1 public skill
    expected_fanout = min(max(3, int(math.sqrt(5))), 5)
    assert len(created_tasks) == expected_fanout, (
        f"Expected {expected_fanout} sends (sqrt fanout x 1 public skill), got {len(created_tasks)}. "
        "Private skill must not be republished."
    )
