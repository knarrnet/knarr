"""Tests for wallet plugin components."""
import os
import pytest
from knarr.plugins.wallet.tx_firewall import TransactionFirewall
from knarr.plugins.wallet.hot import KeypairSigner, WalletSigner
from knarr.core.egress_filter import EgressFilter


class TestKeypairSigner:
    def test_is_wallet_signer(self):
        """KeypairSigner implements WalletSigner ABC."""
        seed = os.urandom(32)
        signer = KeypairSigner(seed)
        assert isinstance(signer, WalletSigner)

    def test_roundtrip(self):
        seed = os.urandom(32)
        signer = KeypairSigner(seed)
        addr = signer.get_address()
        assert addr
        assert 32 <= len(addr) <= 44
        sig = signer.sign_message(b"hello")
        assert len(sig) == 64

    def test_deterministic(self):
        seed = bytes(range(32))
        s1 = KeypairSigner(seed)
        s2 = KeypairSigner(seed)
        assert s1.get_address() == s2.get_address()
        assert s1.sign_message(b"test") == s2.sign_message(b"test")

    def test_bad_seed_length(self):
        with pytest.raises(ValueError, match="32 bytes"):
            KeypairSigner(b"short")


class TestTransactionFirewall:
    def test_allows_valid(self):
        tf = TransactionFirewall("hot_addr", {"peer1", "peer2"})
        tf.set_ledger_lookup(lambda w: -50.0)
        ok, err = tf.check("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "hot_addr", "peer1", 50.0)
        assert ok is True

    def test_blocks_bad_program(self):
        tf = TransactionFirewall("hot_addr", {"peer1"})
        tf.set_ledger_lookup(lambda w: -50.0)
        ok, err = tf.check("BadProgram", "hot_addr", "peer1", 50.0)
        assert ok is False
        assert "program not whitelisted" in err

    def test_blocks_wrong_source(self):
        tf = TransactionFirewall("hot_addr", {"peer1"})
        tf.set_ledger_lookup(lambda w: -50.0)
        ok, err = tf.check("11111111111111111111111111111111", "wrong_src", "peer1", 50.0)
        assert ok is False
        assert "source is not own" in err

    def test_blocks_unknown_dest(self):
        tf = TransactionFirewall("hot_addr", {"peer1"})
        tf.set_ledger_lookup(lambda w: -50.0)
        ok, err = tf.check("11111111111111111111111111111111", "hot_addr", "unknown_peer", 50.0)
        assert ok is False
        assert "not a known peer" in err

    def test_blocks_over_balance(self):
        tf = TransactionFirewall("hot_addr", {"peer1"})
        tf.set_ledger_lookup(lambda w: -50.0)
        ok, err = tf.check("11111111111111111111111111111111", "hot_addr", "peer1", 60.0)
        assert ok is False
        assert "exceeds ledger" in err

    def test_fails_closed_without_ledger_lookup(self):
        """F-2 sentinel: firewall MUST refuse to sign if ledger lookup is not wired."""
        tf = TransactionFirewall("hot_addr", {"peer1"})
        # No set_ledger_lookup call — check #4 must fail closed
        ok, err = tf.check("11111111111111111111111111111111", "hot_addr", "peer1", 10.0)
        assert ok is False
        assert "ledger lookup not wired" in err

    def test_cannot_be_bypassed(self):
        """Sentinel: no bypass/debug/disable paths in firewall check."""
        import inspect
        source = inspect.getsource(TransactionFirewall.check)
        assert "bypass" not in source.lower()
        assert "debug" not in source.lower()
        assert "disable" not in source.lower()


class TestEgressIntegration:
    def test_blocks_hot_key_hex(self):
        """Sentinel: Egress filter blocks hot wallet seed hex in output."""
        ef = EgressFilter()
        seed = os.urandom(32)
        ef.register_sensitive_material(seed)
        assert ef.check(seed.hex()) is False

    def test_blocks_hot_key_base58(self):
        """Sentinel: Egress filter blocks hot wallet seed base58 in output."""
        ef = EgressFilter()
        seed = os.urandom(32)
        ef.register_sensitive_material(seed)
        from knarr.core.wallet import b58encode
        assert ef.check(b58encode(seed)) is False
