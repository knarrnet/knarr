"""Tests for wallet field [E-1]: storage, API exposure."""
import pytest
from knarr.dht.storage import Storage
from knarr.core.models import NodeInfo


class TestWalletStorage:
    def test_wallet_column_exists(self):
        s = Storage(":memory:")
        conn = s._get_conn()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(peers)").fetchall()}
        assert "wallet" in cols

    def test_update_and_get_wallet(self):
        s = Storage(":memory:")
        peer = NodeInfo("node1", "127.0.0.1", 9000)
        s.upsert_peer(peer)
        s.update_peer_wallet("node1", "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU")
        peers = s.get_peers_full()
        assert peers[0]["wallet"] == "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"

    def test_wallet_defaults_empty(self):
        s = Storage(":memory:")
        peer = NodeInfo("node1", "127.0.0.1", 9000)
        s.upsert_peer(peer)
        peers = s.get_peers_full()
        assert peers[0]["wallet"] == ""


class TestWalletInStatus:
    def test_wallet_always_derived(self):
        """Manual wallet config is ignored — peers reject non-derived wallets."""
        from knarr.dht.node import DHTNode
        node = DHTNode("127.0.0.1", 0, config={
            "node": {"wallet": "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"}
        })
        node._start_time = 0
        status = node.get_status()
        # Must use derived address, not the manual one
        assert status["wallet"] != "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
        assert len(status["wallet"]) >= 32
        assert len(status["wallet"]) <= 44

    def test_wallet_auto_derived_when_not_configured(self):
        """Wallet auto-derives from Ed25519 keypair when not manually set."""
        from knarr.dht.node import DHTNode
        node = DHTNode("127.0.0.1", 0, config={"node": {}})
        node._start_time = 0
        status = node.get_status()
        # Auto-derived wallet is a base58-encoded Solana address (32-44 chars)
        assert len(status["wallet"]) >= 32
        assert len(status["wallet"]) <= 44
