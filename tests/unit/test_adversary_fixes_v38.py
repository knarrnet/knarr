"""Seam tests for v0.38.0 adversary-hardened fixes.

Each test targets a specific adversary finding and its fix,
especially testing the seams between components.
"""
import hashlib
import hmac as hmac_mod
import math
import os
import tempfile
import time
import types
import unittest


# ── FIX-001: Migration idempotency ──────────────────────────────────

class TestMigrationIdempotency(unittest.TestCase):
    """FIX-001: run_v038_balance_migration must be idempotent."""

    def _make_storage(self, db_path):
        from knarr.dht.storage import Storage
        return Storage(db_path)

    def test_double_migration_does_not_double_shift(self):
        """Running migration twice must produce same result as once."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            s = self._make_storage(db_path)
            pk = "aa" * 32
            s.get_or_create_ledger_entry(pk)
            # Set initial balance via Storage's own connection
            s._get_conn().execute(
                "UPDATE ledger SET balance = 3.0 WHERE peer_public_key = ?", (pk,)
            )
            s._get_conn().commit()

            # First migration: 3.0 - 3.0 = 0.0
            rows1 = s.run_v038_balance_migration(3.0)
            entry_after_first = s.get_or_create_ledger_entry(pk)
            self.assertGreater(rows1, 0, "first migration should update rows")
            self.assertAlmostEqual(entry_after_first.balance, 0.0, places=4)

            # Second migration: should be skipped (idempotent)
            rows2 = s.run_v038_balance_migration(3.0)
            entry_after_second = s.get_or_create_ledger_entry(pk)
            self.assertEqual(rows2, 0, "second migration should skip (idempotent)")
            self.assertAlmostEqual(entry_after_second.balance, 0.0, places=4,
                                   msg="balance must not shift on second run")
        finally:
            try:
                s._keepalive_conn.close()
            except Exception:
                pass
            os.unlink(db_path)

    def test_triple_restart_stability(self):
        """Simulates 3 node restarts — balance should only shift once."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            s = self._make_storage(db_path)
            pk = "bb" * 32
            s.get_or_create_ledger_entry(pk)
            # Set initial balance via Storage's own connection
            s._get_conn().execute(
                "UPDATE ledger SET balance = 5.0 WHERE peer_public_key = ?", (pk,)
            )
            s._get_conn().commit()

            # 3 "restarts"
            for i in range(3):
                s.run_v038_balance_migration(3.0)

            entry = s.get_or_create_ledger_entry(pk)
            self.assertAlmostEqual(entry.balance, 2.0, places=4,
                                   msg="5.0 - 3.0 = 2.0 regardless of restart count")
        finally:
            try:
                s._keepalive_conn.close()
            except Exception:
                pass
            os.unlink(db_path)


# ── FIX-002: HMAC replay guard ──────────────────────────────────────

def _make_server(send_secret="test-secret", max_per_tx=100.0, max_daily=1000.0, timestamp_window=30):
    """Build a minimal CockpitServer mock with wallet auth wired in."""
    from knarr.dashboard.server import CockpitServer
    import asyncio

    node = types.SimpleNamespace(
        _config={
            "cockpit": {
                "wallet": {
                    "send_secret": send_secret,
                    "max_per_tx": max_per_tx,
                    "max_daily": max_daily,
                    "timestamp_window_seconds": timestamp_window,
                }
            }
        },
        bus=None,
    )

    srv = object.__new__(CockpitServer)
    srv._node = node
    srv._auth_token = ""
    srv._wallet_daily_spent = 0.0
    srv._wallet_daily_reset = time.time()
    srv._seen_wallet_sigs = {}
    srv._sig_sweep_interval = 60.0
    srv._last_sig_sweep = time.time()
    srv._wallet_send_lock = asyncio.Lock()
    return srv


def _sign_request(secret, method, path, body, ts):
    msg = f"{ts}\n{method}\n{path}\n{body}"
    return hmac_mod.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


class TestHMACReplayGuard(unittest.TestCase):
    """FIX-002: Same signature must be rejected on second use."""

    def setUp(self):
        self.srv = _make_server(send_secret="my-secret")

    def _make_headers(self, ts=None):
        ts = ts or int(time.time())
        body = '{"to":"addr","amount":10}'
        sig = _sign_request("my-secret", "POST", "/api/wallet/send", body, ts)
        return {"x-wallet-timestamp": str(ts), "x-wallet-signature": sig}, body

    def test_first_use_passes(self):
        headers, body = self._make_headers()
        self.assertTrue(self.srv._check_wallet_auth("POST", "/api/wallet/send", body, headers))

    def test_replay_rejected(self):
        headers, body = self._make_headers()
        # First use passes
        self.assertTrue(self.srv._check_wallet_auth("POST", "/api/wallet/send", body, headers))
        # Exact same request replayed — must be rejected
        self.assertFalse(self.srv._check_wallet_auth("POST", "/api/wallet/send", body, headers))

    def test_different_timestamps_both_pass(self):
        """Two requests with different timestamps should both pass."""
        ts1 = int(time.time())
        ts2 = ts1 + 1
        h1, body = self._make_headers(ts=ts1)
        h2_sig = _sign_request("my-secret", "POST", "/api/wallet/send", body, ts2)
        h2 = {"x-wallet-timestamp": str(ts2), "x-wallet-signature": h2_sig}
        self.assertTrue(self.srv._check_wallet_auth("POST", "/api/wallet/send", body, h1))
        self.assertTrue(self.srv._check_wallet_auth("POST", "/api/wallet/send", body, h2))


# ── FIX-007: get_or_create fetches extended columns ──────────────────

class TestGetOrCreateExtendedColumns(unittest.TestCase):
    """FIX-007: get_or_create_ledger_entry must fetch stored prepaid/hard_limit."""

    def test_stored_prepaid_returned(self):
        """After update_prepaid, get_or_create returns stored value not default."""
        from knarr.dht.storage import Storage

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            s = Storage(db_path)
            pk = "cc" * 32
            s.get_or_create_ledger_entry(pk)
            s.update_prepaid(pk, 123.0)

            entry = s.get_or_create_ledger_entry(pk)
            self.assertAlmostEqual(entry.prepaid, 123.0, places=2,
                                   msg="must return stored prepaid, not default 0.0")
        finally:
            try:
                s._keepalive_conn.close()
            except Exception:
                pass
            os.unlink(db_path)

    def test_stored_hard_limit_returned(self):
        """Per-peer hard_limit stored in DB must be returned, not dataclass default."""
        from knarr.dht.storage import Storage

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            s = Storage(db_path)
            pk = "dd" * 32
            s.get_or_create_ledger_entry(pk)
            # Set hard_limit via Storage's own connection
            s._get_conn().execute(
                "UPDATE ledger SET hard_limit = -2.0 WHERE peer_public_key = ?", (pk,)
            )
            s._get_conn().commit()

            entry = s.get_or_create_ledger_entry(pk)
            self.assertAlmostEqual(entry.hard_limit, -2.0, places=2,
                                   msg="must return stored hard_limit -2.0, not default -10.0")
        finally:
            try:
                s._keepalive_conn.close()
            except Exception:
                pass
            os.unlink(db_path)


# ── FIX-008: NaN guard on ledger updates ─────────────────────────────

class TestLedgerNaNGuard(unittest.TestCase):
    """FIX-008: update_ledger_provider/consumer must reject NaN/Inf."""

    def _make_storage(self):
        from knarr.dht.storage import Storage
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        s = Storage(db_path)
        pk = "ee" * 32
        s.get_or_create_ledger_entry(pk)
        return s, pk, db_path

    def test_provider_rejects_nan(self):
        s, pk, db_path = self._make_storage()
        try:
            with self.assertRaises(ValueError):
                s.update_ledger_provider(pk, float("nan"))
        finally:
            s._keepalive_conn.close()
            os.unlink(db_path)

    def test_provider_rejects_inf(self):
        s, pk, db_path = self._make_storage()
        try:
            with self.assertRaises(ValueError):
                s.update_ledger_provider(pk, float("inf"))
        finally:
            s._keepalive_conn.close()
            os.unlink(db_path)

    def test_consumer_rejects_nan(self):
        s, pk, db_path = self._make_storage()
        try:
            with self.assertRaises(ValueError):
                s.update_ledger_consumer(pk, float("nan"))
        finally:
            s._keepalive_conn.close()
            os.unlink(db_path)

    def test_consumer_rejects_neg_inf(self):
        s, pk, db_path = self._make_storage()
        try:
            with self.assertRaises(ValueError):
                s.update_ledger_consumer(pk, float("-inf"))
        finally:
            s._keepalive_conn.close()
            os.unlink(db_path)

    def test_provider_accepts_valid_price(self):
        """Finite prices (positive, negative, zero) must still work."""
        s, pk, db_path = self._make_storage()
        try:
            s.update_ledger_provider(pk, 5.0)
            s.update_ledger_provider(pk, -3.0)  # bounty
            s.update_ledger_provider(pk, 0.0)   # free
            entry = s.get_or_create_ledger_entry(pk)
            # balance = 0 - 5 - (-3) - 0 = 0 - 5 + 3 = -2
            self.assertAlmostEqual(entry.balance, -2.0, places=4)
        finally:
            s._keepalive_conn.close()
            os.unlink(db_path)


# ── FIX-009: update_prepaid NaN/negative guard ───────────────────────

class TestPrepaidGuard(unittest.TestCase):
    """FIX-009: update_prepaid must reject NaN, Inf, and negative amounts."""

    def _make_storage(self):
        from knarr.dht.storage import Storage
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        s = Storage(db_path)
        pk = "ff" * 32
        s.get_or_create_ledger_entry(pk)
        return s, pk, db_path

    def test_rejects_nan(self):
        s, pk, db_path = self._make_storage()
        try:
            with self.assertRaises(ValueError):
                s.update_prepaid(pk, float("nan"))
        finally:
            s._keepalive_conn.close()
            os.unlink(db_path)

    def test_rejects_negative(self):
        s, pk, db_path = self._make_storage()
        try:
            with self.assertRaises(ValueError):
                s.update_prepaid(pk, -100.0)
        finally:
            s._keepalive_conn.close()
            os.unlink(db_path)

    def test_accepts_zero(self):
        """Zero prepaid update should be allowed (no-op)."""
        s, pk, db_path = self._make_storage()
        try:
            s.update_prepaid(pk, 0.0)  # should not raise
        finally:
            s._keepalive_conn.close()
            os.unlink(db_path)

    def test_accepts_positive(self):
        s, pk, db_path = self._make_storage()
        try:
            s.update_prepaid(pk, 50.0)
            entry = s.get_or_create_ledger_entry(pk)
            self.assertAlmostEqual(entry.prepaid, 50.0, places=2)
        finally:
            s._keepalive_conn.close()
            os.unlink(db_path)


# ── FIX-006: Chain_id validation (empty bypass) ──────────────────────

class TestChainIdValidation(unittest.TestCase):
    """FIX-006: Empty or mismatched chain_id must be rejected in netting handlers."""

    def test_empty_chain_id_rejected_in_proposal(self):
        """Netting proposal with empty chain_id must be rejected."""
        from knarr.commerce.handlers import make_commerce_handlers

        accepted = []
        node = self._make_mock_node(accepted)
        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/netting_proposal"]

        import asyncio
        item = {
            "from_node": "a" * 64,
            "body": {
                "netting_id": "test-netting-001",
                "settlement_amount": 10.0,
                "chain_id": "",  # empty — should be rejected
            },
        }
        asyncio.get_event_loop().run_until_complete(handler(item))
        self.assertEqual(len(accepted), 0, "empty chain_id should be rejected")

    def test_wrong_chain_id_rejected(self):
        """Netting proposal with wrong chain_id must be rejected."""
        from knarr.commerce.handlers import make_commerce_handlers

        accepted = []
        node = self._make_mock_node(accepted)
        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/netting_proposal"]

        import asyncio
        item = {
            "from_node": "a" * 64,
            "body": {
                "netting_id": "test-netting-002",
                "settlement_amount": 10.0,
                "chain_id": "evm-mainnet",  # wrong chain
            },
        }
        asyncio.get_event_loop().run_until_complete(handler(item))
        self.assertEqual(len(accepted), 0, "wrong chain_id should be rejected")

    def _make_mock_node(self, accepted_list):
        """Create minimal mock node for handler testing."""
        return types.SimpleNamespace(
            _config={"blockchain": {"chain": "solana-devnet", "token_mint": "TEST"}},
            node_info=types.SimpleNamespace(node_id="b" * 64),
            storage=types.SimpleNamespace(
                get_ledger_balance=lambda pk: -5.0,
                get_all_ledger_entries=lambda: [],
            ),
            _sync=types.SimpleNamespace(
                enqueue=lambda **kwargs: accepted_list.append(kwargs),
            ),
            bus=None,
        )


# ── FIX-004/005: Netting session verification ────────────────────────

class TestNettingSessionStore(unittest.TestCase):
    """FIX-004/005: handle_netting_acceptance must verify session exists and reject replays."""

    def test_acceptance_without_session_rejected(self):
        """Acceptance for a netting_id with no prior proposal must be rejected."""
        from knarr.commerce.handlers import make_commerce_handlers
        import asyncio

        sends = []
        node = types.SimpleNamespace(
            _config={"blockchain": {"chain": "solana-devnet"}},
            node_info=types.SimpleNamespace(node_id="b" * 64),
            storage=types.SimpleNamespace(
                get_ledger_balance=lambda pk: -5.0,
                get_all_ledger_entries=lambda: [],
            ),
            _sync=types.SimpleNamespace(
                enqueue=self._async_append(sends),
            ),
            bus=None,
            call_local=self._async_noop(),
        )

        handlers = make_commerce_handlers(node)
        handler = handlers["knarr/commerce/netting_acceptance"]

        item = {
            "from_node": "a" * 64,
            "body": {
                "netting_id": "forged-netting-999",
                "proposal_ref": "np_fake",
                "accepted_amount": 77.7,
                "source_address": "ATTACKER_WALLET",
            },
        }
        asyncio.get_event_loop().run_until_complete(handler(item))
        # No on-chain send should have been attempted
        self.assertEqual(len(sends), 0, "acceptance without session must be rejected")

    def _async_append(self, lst):
        async def _enqueue(**kwargs):
            lst.append(kwargs)
        return _enqueue

    def _async_noop(self):
        async def _noop(*args, **kwargs):
            return {"tx_hash": "fake"}
        return _noop


if __name__ == "__main__":
    unittest.main()
