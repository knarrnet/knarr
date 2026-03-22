"""BUG-01: _reannounce_all() sends to all peers, not a fanout sample.

BUG: scheduled 300s republish used random.sample(peers, fanout=3), causing
sparse coverage at scale (100-node: median node knows only 44/100 providers).

FIX: _reannounce_all() iterates all peers. announce() hot-path still uses fanout=3.
"""
import asyncio
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

    _bind_reannounce(node)
    return node, peers


@pytest.mark.asyncio
async def test_reannounce_all_sends_to_all_peers():
    """_reannounce_all() must send to ALL peers, not a fanout sample."""
    from knarr.core.models import SkillSheet

    node, peers = _make_node_stub(peer_count=10)

    skill = MagicMock(spec=SkillSheet)
    skill.name = "test-skill"
    skill.to_dict.return_value = {"name": "test-skill"}
    node._own_skills = {"test-skill": skill}
    node._skill_visibility = {"test-skill": "public"}

    sent_to = []

    async def fake_send(peer, msg):
        sent_to.append(peer.node_id)

    node._send_to_peer = fake_send

    with patch("asyncio.create_task", side_effect=lambda coro: asyncio.get_event_loop().create_task(coro)):
        await node._reannounce_all()
        await asyncio.sleep(0)

    assert len(sent_to) == 10, (
        f"_reannounce_all() sent to {len(sent_to)} peers, expected 10 (all peers). "
        "BUG-01: must send to ALL peers on scheduled republish, not a fanout sample."
    )


@pytest.mark.asyncio
async def test_reannounce_all_exceeds_fanout():
    """_reannounce_all() with 20 peers must NOT limit sends to fanout=3."""
    from knarr.core.models import SkillSheet

    node, peers = _make_node_stub(peer_count=20)

    skill = MagicMock(spec=SkillSheet)
    skill.name = "test-skill"
    skill.to_dict.return_value = {"name": "test-skill"}
    node._own_skills = {"test-skill": skill}
    node._skill_visibility = {"test-skill": "public"}

    sent_to = []

    async def fake_send(peer, msg):
        sent_to.append(peer.node_id)

    node._send_to_peer = fake_send

    with patch("asyncio.create_task", side_effect=lambda coro: asyncio.get_event_loop().create_task(coro)):
        await node._reannounce_all()
        await asyncio.sleep(0)

    assert len(sent_to) == 20, (
        f"_reannounce_all() sent to {len(sent_to)} peers, expected 20 (all peers). "
        f"fanout={node._gossip_fanout} must NOT limit republish."
    )
    assert len(sent_to) > node._gossip_fanout


@pytest.mark.asyncio
async def test_reannounce_all_skips_private_skills():
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

    announced_msgs = []

    async def fake_send(peer, msg):
        pass

    node._send_to_peer = fake_send
    created_tasks = []

    with patch("asyncio.create_task", side_effect=lambda coro: (created_tasks.append(coro), asyncio.get_event_loop().create_task(coro))[1]):
        await node._reannounce_all()
        await asyncio.sleep(0)

    # 5 peers x 1 public skill = 5 sends (not 10 which would include private)
    assert len(created_tasks) == 5, (
        f"Expected 5 sends (5 peers x 1 public skill), got {len(created_tasks)}. "
        "Private skill must not be republished."
    )
