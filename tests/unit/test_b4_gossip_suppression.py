"""B4 contract test: D-007 Phase C — gossip suppression.

D-007 Phases A (passive cache) and B (active routing) shipped. Phase C gossip
suppression is one missing line. The on_outbound hook in the kademlia plugin
always returns True (propagate gossip). When the plugin places a provider record
via PUT, it should return False to suppress O(n) gossip flood.

Without suppression:
- Every Announce triggers gossip to all peers (O(n) messages)
- At 500 nodes this is 250,000 messages per announce
- KAD exists to replace this with O(log n) routing — but gossip still fires

FIX LOCATION: plugins/00-kademlia/handler.py:on_outbound()
When an Announce for our own skill is processed in "full" mode AND the PUT
task is scheduled, return False instead of True:

    if self._lookup and self.mode == "full" and visibility == "public":
        asyncio.create_task(self._put_provider_to_closest(skill_key))
        return False  # <-- Phase C: suppress gossip, KAD handles distribution

CONTRACT:
- on_outbound() for an Announce of our own public skill in "full" mode must
  return False (gossip suppressed).
- on_outbound() for an Announce of our own private skill must return True
  (gossip not suppressed — KAD doesn't handle private skills).
- on_outbound() for an Announce from a DIFFERENT node must return True
  (we only suppress our own announces).
- on_outbound() in "passive" mode must return True (KAD passive can't PUT).
- on_outbound() for non-Announce messages must return True (no regression).
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


def _import_kad_handler():
    import sys, pathlib, importlib.util
    plugin_path = str(pathlib.Path(__file__).parents[2] / "plugins" / "00-kademlia")
    if plugin_path not in sys.path:
        sys.path.insert(0, plugin_path)
    sys.modules.pop("handler", None)
    from handler import KademliaPlugin
    return KademliaPlugin


def _make_kad(mode="full", our_node_id="aa" * 32):
    KademliaPlugin = _import_kad_handler()

    ctx = MagicMock()
    ctx.node_id = our_node_id
    ctx.plugin_dir = MagicMock()
    ctx.plugin_dir.__truediv__ = lambda self, other: MagicMock()
    ctx.send_plugin_message = AsyncMock()
    ctx.subscribe_events = None

    config = {"mode": mode, "k": 20, "alpha": 3, "debug": False}

    plugin = KademliaPlugin.__new__(KademliaPlugin)
    plugin._ctx = ctx
    plugin._log = MagicMock()
    plugin._debug = False
    plugin.mode = mode
    plugin.k = 20
    plugin.kbuckets = MagicMock()
    plugin.providers = MagicMock()
    plugin._lookup = MagicMock() if mode == "full" else None
    plugin._is_valid_hex_id = lambda x: len(x) == 64 and all(c in "0123456789abcdef" for c in x)
    return plugin


def _make_announce(node_id, skill_key="knarr:///cat/sub/skill@1.0", visibility="public"):
    from knarr.core.messages import Announce
    msg = MagicMock(spec=Announce)
    msg.node_id = node_id
    msg.skill_key = skill_key
    msg.sidecar_port = 0
    msg.skill_sheet = {"visibility": visibility}
    return msg


def _make_peer(node_id="bb" * 32):
    from knarr.core.models import NodeInfo
    peer = MagicMock(spec=NodeInfo)
    peer.node_id = node_id
    peer.host = "10.0.0.1"
    peer.port = 9010
    return peer


OUR_NODE_ID = "aa" * 32


@pytest.mark.asyncio
async def test_own_public_announce_full_mode_suppresses_gossip():
    """Own public announce in full mode must return False (gossip suppressed)."""
    plugin = _make_kad(mode="full", our_node_id=OUR_NODE_ID)
    msg = _make_announce(OUR_NODE_ID, visibility="public")
    peer = _make_peer()

    with patch("asyncio.create_task"):
        result = await plugin.on_outbound(msg, peer)

    assert result is False, (
        f"on_outbound returned {result} for own public announce in full mode. "
        "Fix: return False after scheduling _put_provider_to_closest to suppress gossip. "
        "This is D-007 Phase C — one missing 'return False' line."
    )


@pytest.mark.asyncio
async def test_own_private_announce_does_not_suppress():
    """Private skill announce must not suppress gossip (KAD doesn't handle private)."""
    plugin = _make_kad(mode="full", our_node_id=OUR_NODE_ID)
    msg = _make_announce(OUR_NODE_ID, visibility="private")
    peer = _make_peer()

    result = await plugin.on_outbound(msg, peer)

    assert result is True, (
        f"on_outbound returned {result} for private skill announce. "
        "Private skills must use gossip — do not suppress."
    )


@pytest.mark.asyncio
async def test_foreign_announce_does_not_suppress():
    """Announce from another node must not suppress gossip."""
    plugin = _make_kad(mode="full", our_node_id=OUR_NODE_ID)
    other_node_id = "bb" * 32
    msg = _make_announce(other_node_id, visibility="public")
    peer = _make_peer()

    result = await plugin.on_outbound(msg, peer)

    assert result is True, (
        f"on_outbound returned {result} for foreign node announce. "
        "Only suppress gossip for our own announces, not forwards."
    )


@pytest.mark.asyncio
async def test_passive_mode_does_not_suppress():
    """Passive mode cannot PUT, so must not suppress gossip."""
    plugin = _make_kad(mode="passive", our_node_id=OUR_NODE_ID)
    msg = _make_announce(OUR_NODE_ID, visibility="public")
    peer = _make_peer()

    result = await plugin.on_outbound(msg, peer)

    assert result is True, (
        f"on_outbound returned {result} for own announce in passive mode. "
        "Passive mode doesn't PUT — must not suppress gossip."
    )


@pytest.mark.asyncio
async def test_non_announce_message_does_not_suppress():
    """Non-Announce messages must always return True (no regression)."""
    from knarr.core.messages import Heartbeat
    plugin = _make_kad(mode="full", our_node_id=OUR_NODE_ID)

    msg = MagicMock(spec=Heartbeat)  # Not an Announce
    peer = _make_peer()

    result = await plugin.on_outbound(msg, peer)

    assert result is True, (
        f"on_outbound returned {result} for non-Announce message. "
        "Only Announce messages should be suppressed."
    )
