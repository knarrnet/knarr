"""Tests for A3.2: HMAC wallet auth, spending caps, daily cap."""
import hashlib
import hmac
import math
import time
import types
import unittest


def _make_server(send_secret="test-secret", max_per_tx=100.0, max_daily=1000.0, timestamp_window=30):
    """Build a minimal CockpitServer mock with wallet auth wired in."""
    from knarr.dashboard.server import CockpitServer

    # Minimal node mock
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

    # We need a minimal server object — bypass __init__ with manual construction
    srv = object.__new__(CockpitServer)
    srv._node = node
    srv._auth_token = ""
    srv._wallet_daily_spent = 0.0
    srv._wallet_daily_reset = time.time()
    srv._seen_wallet_sigs = {}
    srv._sig_sweep_interval = 60.0
    srv._last_sig_sweep = time.time()
    return srv


def _sign_request(secret: str, method: str, path: str, body: str, ts: int) -> str:
    msg = f"{ts}\n{method}\n{path}\n{body}"
    return hmac.new(
        secret.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class TestWalletHMACAuth(unittest.TestCase):

    def setUp(self):
        self.srv = _make_server(send_secret="my-secret")

    def _make_headers(self, secret, method, path, body, ts=None, sig_override=None):
        ts = ts or int(time.time())
        sig = sig_override or _sign_request(secret, method, path, body, ts)
        return {
            "x-wallet-timestamp": str(ts),
            "x-wallet-signature": sig,
        }

    def test_valid_hmac_passes(self):
        body = '{"to":"addr","amount":10}'
        headers = self._make_headers("my-secret", "POST", "/api/wallet/send", body)
        self.assertTrue(self.srv._check_wallet_auth("POST", "/api/wallet/send", body, headers))

    def test_wrong_secret_fails(self):
        body = '{"to":"addr","amount":10}'
        headers = self._make_headers("wrong-secret", "POST", "/api/wallet/send", body)
        self.assertFalse(self.srv._check_wallet_auth("POST", "/api/wallet/send", body, headers))

    def test_expired_timestamp_fails(self):
        body = '{"to":"addr","amount":10}'
        old_ts = int(time.time()) - 60  # 60s ago, window is 30s
        headers = self._make_headers("my-secret", "POST", "/api/wallet/send", body, ts=old_ts)
        self.assertFalse(self.srv._check_wallet_auth("POST", "/api/wallet/send", body, headers))

    def test_future_timestamp_fails(self):
        body = '{"to":"addr","amount":10}'
        future_ts = int(time.time()) + 60
        headers = self._make_headers("my-secret", "POST", "/api/wallet/send", body, ts=future_ts)
        self.assertFalse(self.srv._check_wallet_auth("POST", "/api/wallet/send", body, headers))

    def test_missing_timestamp_fails(self):
        body = '{"to":"addr","amount":10}'
        ts = int(time.time())
        sig = _sign_request("my-secret", "POST", "/api/wallet/send", body, ts)
        headers = {"x-wallet-signature": sig}  # no timestamp
        self.assertFalse(self.srv._check_wallet_auth("POST", "/api/wallet/send", body, headers))

    def test_missing_signature_fails(self):
        ts = int(time.time())
        headers = {"x-wallet-timestamp": str(ts)}  # no signature
        self.assertFalse(self.srv._check_wallet_auth("POST", "/api/wallet/send", b"", headers))

    def test_tampered_body_fails(self):
        body = '{"to":"addr","amount":10}'
        headers = self._make_headers("my-secret", "POST", "/api/wallet/send", body)
        # Tamper body after signing
        tampered = '{"to":"attacker","amount":10}'
        self.assertFalse(self.srv._check_wallet_auth("POST", "/api/wallet/send", tampered, headers))

    def test_no_secret_configured_fails(self):
        srv = _make_server(send_secret="")
        body = '{"to":"addr","amount":10}'
        headers = self._make_headers("anything", "POST", "/api/wallet/send", body)
        self.assertFalse(srv._check_wallet_auth("POST", "/api/wallet/send", body, headers))

    def test_bytes_body_accepted(self):
        body = b'{"to":"addr","amount":10}'
        ts = int(time.time())
        sig = _sign_request("my-secret", "POST", "/api/wallet/send", body.decode(), ts)
        headers = {"x-wallet-timestamp": str(ts), "x-wallet-signature": sig}
        self.assertTrue(self.srv._check_wallet_auth("POST", "/api/wallet/send", body, headers))


class TestWalletSpendCap(unittest.TestCase):

    def setUp(self):
        self.srv = _make_server(max_per_tx=100.0, max_daily=500.0)

    def test_within_per_tx_cap_passes(self):
        ok, reason = self.srv._check_wallet_spend_cap(50.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_exceeds_per_tx_cap_fails(self):
        ok, reason = self.srv._check_wallet_spend_cap(150.0)
        self.assertFalse(ok)
        self.assertIn("per-tx", reason)

    def test_daily_cap_accumulates(self):
        self.srv._wallet_daily_spent = 450.0
        ok, reason = self.srv._check_wallet_spend_cap(60.0)
        self.assertFalse(ok)
        self.assertIn("daily", reason)

    def test_daily_cap_resets_after_24h(self):
        self.srv._wallet_daily_spent = 490.0
        self.srv._wallet_daily_reset = time.time() - 90000  # > 24h ago
        ok, _ = self.srv._check_wallet_spend_cap(50.0)
        # After reset, 50 < 500 → allowed
        self.assertTrue(ok)

    def test_exact_daily_cap_boundary(self):
        self.srv._wallet_daily_spent = 499.0
        ok, _ = self.srv._check_wallet_spend_cap(1.0)  # 500 == max → allowed
        self.assertTrue(ok)

    def test_over_daily_cap_by_one_cent(self):
        self.srv._wallet_daily_spent = 499.99
        ok, _ = self.srv._check_wallet_spend_cap(1.0)  # 500.99 > 500 → blocked
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
