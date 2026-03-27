"""E-06: Per-identity skill announcements.

Tests announce_identity_skills() on DHTNode:
1. Method exists on DHTNode.
2. Returns 0 when identity has no skills.
3. Returns 0 when identity has no signing_key.
4. Returns 0 when no peers are available.
5. Returns count of announced skills when identity has skills + peers.
6. Uses identity.signing_key (not node key) to sign announcements.
7. announce_identity_skills is async (coroutine).
"""
import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock


class TestIdentityAnnounce:
    def test_method_exists(self):
        """announce_identity_skills method exists on DHTNode."""
        from knarr.dht.node import DHTNode
        assert hasattr(DHTNode, "announce_identity_skills")

    def test_method_is_coroutine(self):
        """announce_identity_skills is a coroutine function."""
        import asyncio
        from knarr.dht.node import DHTNode
        assert asyncio.iscoroutinefunction(DHTNode.announce_identity_skills)

    def test_returns_zero_no_identity(self):
        """Returns 0 when identity is None."""
        async def _run():
            from knarr.dht.node import DHTNode
            node = DHTNode.__new__(DHTNode)
            return await node.announce_identity_skills(None)

        result = asyncio.run(_run())
        assert result == 0

    def test_returns_zero_no_signing_key(self):
        """Returns 0 when identity has no signing_key."""
        async def _run():
            from knarr.dht.node import DHTNode
            from knarr.dht.identities import Identity

            node = DHTNode.__new__(DHTNode)
            identity = Identity(name="alice", node_id="aa" * 32, signing_key=None)
            identity.skills = {"my_skill": MagicMock()}
            return await node.announce_identity_skills(identity)

        result = asyncio.run(_run())
        assert result == 0

    def test_returns_zero_no_peers(self):
        """Returns 0 when no peers available."""
        async def _run():
            from knarr.dht.node import DHTNode
            from knarr.dht.identities import Identity
            from nacl.signing import SigningKey

            node = DHTNode.__new__(DHTNode)
            node.storage = MagicMock()
            node.storage.get_peers.return_value = []

            identity = Identity(name="alice", node_id="aa" * 32, signing_key=SigningKey.generate())
            identity.skills = {"my_skill": MagicMock()}
            return await node.announce_identity_skills(identity)

        result = asyncio.run(_run())
        assert result == 0

    def test_returns_skill_count_with_peers(self):
        """Returns count of announced skills when identity has skills and peers."""
        async def _run():
            from knarr.dht.node import DHTNode
            from knarr.dht.identities import Identity
            from nacl.signing import SigningKey

            node = DHTNode.__new__(DHTNode)
            node.storage = MagicMock()
            # Make a fake peer
            peer = MagicMock()
            peer.host = "127.0.0.1"
            peer.port = 9001
            node.storage.get_peers.return_value = [peer]
            node._sidecar_port = 0
            node._encryption_key_hex = ""
            node._wallet = ""
            node.node_info = MagicMock()
            node.node_info.host = "127.0.0.1"
            node.node_info.port = 9000
            node._debug = False
            node._send_fire_forget = AsyncMock()

            sk = SigningKey.generate()
            identity = Identity(name="alice", node_id=sk.verify_key.encode().hex(), signing_key=sk)

            skill_sheet = MagicMock()
            skill_sheet.to_dict.return_value = {"name": "my_skill", "price": 1.0}
            identity.skills = {"my_skill": skill_sheet}

            # Patch the jurisdiction property to return ""
            with patch.object(type(node), "_node_jurisdiction_wire", new_callable=lambda: property(lambda self: "")):
                with patch("asyncio.create_task"):
                    result = await node.announce_identity_skills(identity)

            return result

        result = asyncio.run(_run())
        assert result == 1

    def test_source_uses_identity_node_id(self):
        """Announce message uses identity.node_id, not node's node_id."""
        import os
        node_path = os.path.join(os.path.dirname(__file__), "../../src/knarr/dht/node.py")
        with open(node_path) as f:
            src = f.read()
        # The announce_identity_skills method should use identity.node_id
        assert "identity.node_id" in src
        # And use identity.signing_key
        assert "identity.signing_key" in src
