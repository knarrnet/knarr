"""A-05: Prune timeout scaling with config lever (prune_timeout_multiplier).

Tests:
1. Default multiplier (1.0) — base timeout unchanged.
2. Configured multiplier (3.0) — base timeout tripled.
3. Network-size scaling still applies (small/medium/large).
4. Config lever stacks with network-size scaling.

Post-assembly item 3: heuristic replaced with config knob per Patrick's directive.
"""
import pytest
from unittest.mock import MagicMock


def _make_node_with_peers(peers_list, multiplier=None):
    """Create a minimal DHTNode-like object to test _get_prune_timeout."""
    from knarr.dht.node import DHTNode

    node = MagicMock(spec=DHTNode)
    node._debug = False
    if multiplier is not None:
        node._config = {"node": {"prune_timeout_multiplier": multiplier}}
    else:
        node._config = {}
    node.storage = MagicMock()
    node.storage.get_peers.return_value = peers_list
    # A-05 extracted _prune_timeout_for_peer_count as a helper; bind the real
    # implementation so MagicMock(spec=...) doesn't shadow it with a stub return.
    node._prune_timeout_for_peer_count = lambda n: DHTNode._prune_timeout_for_peer_count(node, n)
    return node


def _make_peer(ip, port=9000, node_id=None):
    from knarr.core.models import NodeInfo
    nid = node_id or ("aa" * 32)
    return NodeInfo(node_id=nid, host=ip, port=port)


class TestPruneTopologyConfigLever:
    def test_default_multiplier_no_change(self):
        """Default multiplier (1.0) — base timeout unchanged."""
        from knarr.dht.node import DHTNode, PEER_DEAD_TIMEOUT
        peers = [_make_peer("172.20.0.1", 9000 + i, f"{i:064x}") for i in range(10)]
        node = _make_node_with_peers(peers)

        result = DHTNode._get_prune_timeout(node)
        # peer_count = 10 < 20 → base = PEER_DEAD_TIMEOUT; default multiplier 1.0
        assert result == PEER_DEAD_TIMEOUT

    def test_multiplier_3x(self):
        """Configured multiplier 3.0 — base timeout tripled (Docker bridge use case)."""
        from knarr.dht.node import DHTNode, PEER_DEAD_TIMEOUT
        peers = [_make_peer("172.20.0.1", 9000 + i, f"{i:064x}") for i in range(10)]
        node = _make_node_with_peers(peers, multiplier=3.0)

        result = DHTNode._get_prune_timeout(node)
        assert result == PEER_DEAD_TIMEOUT * 3.0

    def test_medium_cluster_with_multiplier(self):
        """Medium cluster (20-49 peers) with multiplier 2.0."""
        from knarr.dht.node import DHTNode, PEER_DEAD_TIMEOUT
        peers = [_make_peer("10.0.0.1", 9000 + i, f"{i:064x}") for i in range(25)]
        node = _make_node_with_peers(peers, multiplier=2.0)

        result = DHTNode._get_prune_timeout(node)
        # peer_count = 25 → base = PEER_DEAD_TIMEOUT * 1.5; multiplier = 2.0
        assert result == PEER_DEAD_TIMEOUT * 1.5 * 2.0

    def test_large_cluster_default(self):
        """Large cluster (50+ peers) with default multiplier."""
        from knarr.dht.node import DHTNode, PEER_DEAD_TIMEOUT
        peers = [_make_peer(f"10.0.0.{i % 256}", 9000, f"{i:064x}") for i in range(60)]
        node = _make_node_with_peers(peers)

        result = DHTNode._get_prune_timeout(node)
        # peer_count = 60 → base = PEER_DEAD_TIMEOUT * 2; multiplier = 1.0
        assert result == PEER_DEAD_TIMEOUT * 2.0

    def test_empty_peers(self):
        """No peers — base timeout (small network bucket)."""
        from knarr.dht.node import DHTNode, PEER_DEAD_TIMEOUT
        node = _make_node_with_peers([])

        result = DHTNode._get_prune_timeout(node)
        assert result == PEER_DEAD_TIMEOUT
