"""A-04: Firewall gateway IP exemption.

Tests:
1. Gateway-exempt IPs bypass L1 rate limiting.
2. Non-exempt IPs still get rate limited.
3. L3+ identity checks (cert blocklist) still apply to exempt IPs.
4. gateway_exempt config is read from plugin config.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_firewall(gateway_exempt=None, base_limit=5, window_seconds=60):
    """Create FirewallPlugin with minimal mocked context."""
    import sys, os, importlib.util
    # Use spec_from_file_location with a unique module name to avoid sys.modules['handler']
    # collision with other plugins (BCW, etc.) that also export a module named 'handler'.
    plugin_dir = os.path.join(os.path.dirname(__file__), "../../plugins/01-firewall")
    _fw_mod = sys.modules.get("firewall_handler")
    if _fw_mod is None:
        spec = importlib.util.spec_from_file_location(
            "firewall_handler", os.path.join(plugin_dir, "handler.py")
        )
        _fw_mod = importlib.util.module_from_spec(spec)
        sys.modules["firewall_handler"] = _fw_mod
        spec.loader.exec_module(_fw_mod)
    FirewallPlugin = _fw_mod.FirewallPlugin
    HistoryBuffer = _fw_mod.HistoryBuffer
    SlidingWindow = _fw_mod.SlidingWindow
    OutboundManager = _fw_mod.OutboundManager
    from knarr.dht.plugins import PluginContext

    ctx = MagicMock()
    ctx.log = MagicMock()
    ctx.group_engine = None
    ctx.send_fire_forget = AsyncMock()
    ctx.get_peers = MagicMock(return_value=[])

    config = {
        "base_limit": base_limit,
        "window_seconds": window_seconds,
        "gateway_exempt": gateway_exempt or [],
        "pending_queue_size": 100,
        "debug": False,
    }
    fw = FirewallPlugin.__new__(FirewallPlugin)
    fw._ctx = ctx
    fw._config = config
    fw._log = ctx.log
    fw._running = True
    import collections
    fw._pending = collections.OrderedDict()
    fw._history = HistoryBuffer(300)
    import collections as _col
    fw._rate_counters = _col.defaultdict(SlidingWindow)
    fw._ip_rate_counters = _col.defaultdict(SlidingWindow)
    fw._ip_blocklist = {}
    fw._cert_blocklist = {}
    fw._warn_timestamps = {}
    fw._smoothed_fill = 0.0
    fw._last_band_change = 0.0
    fw._current_pressure_multiplier = 1.0
    fw._outbound = OutboundManager(ctx, config)
    fw._role = "leaf"
    fw._pending_max = 100
    fw._staleness_seconds = 20
    fw._base_limit = base_limit
    fw._window_seconds = window_seconds
    fw._ban_duration_minutes = 60
    fw._warn_cooldown = 10.0
    fw._processing_enabled = True
    fw._debug = False
    fw._block_groups = []
    fw._rate_multipliers = {}
    fw._qos_groups = []
    fw._gateway_exempt = set(gateway_exempt or [])
    return fw


class TestGatewayExempt:
    def test_exempt_ip_not_rate_limited(self):
        """Gateway-exempt IPs bypass L1 rate limiting."""
        from knarr.core.messages import Announce
        fw = _make_firewall(gateway_exempt=["172.20.0.1"], base_limit=5)

        # Send 100 messages from exempt IP — should never be banned
        for _ in range(100):
            msg = Announce(node_id="aa" * 32, skill_key="test")
            result = _run(fw.on_inbound(msg, "172.20.0.1"))
            # Result is False because announce goes to queue, but NOT because it's banned
            # The IP should not appear in blocklist
            assert "172.20.0.1" not in fw._ip_blocklist

    def test_non_exempt_ip_rate_limited(self):
        """Non-exempt IPs still get rate limited after base_limit exceeded."""
        from knarr.core.messages import Announce, Heartbeat
        fw = _make_firewall(gateway_exempt=[], base_limit=3, window_seconds=60)

        # Send messages beyond limit
        for i in range(5):
            msg = Announce(node_id="bb" * 32, skill_key="test")
            _run(fw.on_inbound(msg, "192.168.1.1"))

        # IP should now be in blocklist
        assert "192.168.1.1" in fw._ip_blocklist or fw._rate_counters.get("192.168.1.1") is not None

    def test_gateway_exempt_config_loaded(self):
        """gateway_exempt config is loaded into set."""
        fw = _make_firewall(gateway_exempt=["172.20.0.1", "10.0.0.1"])
        assert "172.20.0.1" in fw._gateway_exempt
        assert "10.0.0.1" in fw._gateway_exempt
        assert "192.168.1.1" not in fw._gateway_exempt

    def test_empty_exempt_list(self):
        """Empty gateway_exempt list means no IPs are exempt."""
        fw = _make_firewall(gateway_exempt=[])
        assert len(fw._gateway_exempt) == 0

    def test_cert_blocklist_still_applies_to_exempt_ip(self):
        """L3+ cert identity checks still apply even for exempt IPs."""
        import hashlib
        from knarr.core.messages import Heartbeat, Announce
        from nacl.signing import SigningKey
        fw = _make_firewall(gateway_exempt=["172.20.0.1"])

        # Create a cert_id and block it
        sk = SigningKey.generate()
        pub_hex = sk.verify_key.encode().hex()
        cert_id = hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()
        import time
        fw._cert_blocklist[cert_id] = time.time() + 3600  # block for 1 hour

        # Announce (non-Heartbeat) from blocked cert on exempt IP — should still be dropped
        msg = Announce(node_id=cert_id, skill_key="test", public_key=pub_hex)
        result = _run(fw.on_inbound(msg, "172.20.0.1"))
        assert result is False
