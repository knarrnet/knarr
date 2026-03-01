import asyncio
import logging
import pytest
import sqlite3
import time
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

# Add firewall plugin dir to sys.path so we can import handler
plugin_path = Path(__file__).parents[2] / "plugins" / "01-firewall"
sys.path.insert(0, str(plugin_path))

from knarr.core.messages import (
    Message, NodeInfo, Heartbeat, Announce, SyncRequest, SyncResponse,
    TaskRequest, TaskResult, TaskStatus, JoinRequest, Query, Warn, Blocked
)
from knarr.dht.plugins import PluginContext, NodeHealth
from handler import FirewallPlugin

@pytest.fixture
def firewall_ctx(tmp_path):
    plugin_dir = tmp_path / "firewall"
    plugin_dir.mkdir()
    ctx = PluginContext(
        node_id="test-node",
        plugin_dir=plugin_dir,
        get_peers=MagicMock(return_value=[]),
        send_to_peer=AsyncMock(),
        send_fire_forget=AsyncMock(),
        delivery_cb=AsyncMock(),
        log=logging.getLogger("test-firewall")
    )
    return ctx

def _healthy() -> NodeHealth:
    return NodeHealth(
        event_loop_lag_ms=1.0, active_connections=2, max_connections=64,
        write_queue_depth=0, peer_count=1, uptime_seconds=100.0,
    )

# ---- SENTINEL TESTS ----

@pytest.mark.asyncio
async def test_L0_drops_banned_ip_before_parsing(firewall_ctx):
    plugin = FirewallPlugin(firewall_ctx, {"ban_duration_minutes": 60})
    plugin._ip_blocklist["1.2.3.4"] = (time.time() + 3600, "attacker")
    
    assert await plugin.on_connect("1.2.3.4") is False
    assert await plugin.on_connect("1.1.1.1") is True

@pytest.mark.asyncio
async def test_L4_bans_at_100pct_regardless_of_pressure(firewall_ctx):
    # Empty queue (pressure=0), but exceed base_limit
    config = {"base_limit": 5, "window_seconds": 60, "ban_duration_minutes": 60}
    plugin = FirewallPlugin(firewall_ctx, config)
    
    for _ in range(5):
        await plugin.on_inbound(Announce(node_id="bad"), "1.1.1.1")
    
    # 6th message triggers ban (no public_key → IP-only ban)
    assert await plugin.on_inbound(Announce(node_id="bad"), "1.1.1.1") is False
    assert "1.1.1.1" in plugin._ip_blocklist

@pytest.mark.asyncio
async def test_dedup_refreshes_last_seen_not_drops(firewall_ctx):
    plugin = FirewallPlugin(firewall_ctx, {"pending_queue_size": 100})
    plugin._processing_enabled = False
    msg = Announce(node_id="p1", skill_key="s1")
    
    await plugin.on_inbound(msg, "1.1.1.1")
    key = plugin._get_dedup_key(msg, "p1", "1.1.1.1")
    item = plugin._pending[key]
    first_seen = item.last_seen
    
    await asyncio.sleep(0.01)
    await plugin.on_inbound(msg, "1.1.1.1")
    assert item.last_seen > first_seen
    assert item.duplicate_count == 1
    assert len(plugin._pending) == 1

@pytest.mark.asyncio
async def test_dedup_still_counts_toward_rate(firewall_ctx):
    config = {"base_limit": 2, "window_seconds": 60}
    plugin = FirewallPlugin(firewall_ctx, config)
    plugin._processing_enabled = False
    msg = Announce(node_id="p1", skill_key="s1")
    
    await plugin.on_inbound(msg, "1.1.1.1") # count 1
    await plugin.on_inbound(msg, "1.1.1.1") # count 2 (dedup hit but counts toward rate)
    
    # 3rd is ban
    assert await plugin.on_inbound(msg, "1.1.1.1") is False

@pytest.mark.asyncio
async def test_warn_bypasses_outbound_queue(firewall_ctx):
    plugin = FirewallPlugin(firewall_ctx, {})
    plugin._processing_enabled = False
    peer = NodeInfo(node_id="peer", host="1.1.1.1", port=1)
    plugin._outbound.set_warn("peer", delay_ms=1000) # Heavy throttle

    # First message should pass and record sent_buffer
    await plugin.on_outbound(Announce(), peer)
    firewall_ctx.send_fire_forget.assert_called_once()
    firewall_ctx.send_fire_forget.reset_mock()

    # Second message should be queued
    await plugin.on_outbound(Announce(), peer)
    assert len(plugin._outbound._penalty_queue) == 1

    firewall_ctx.send_fire_forget.assert_not_called()

    # Control message should bypass
    warn_msg = Warn(delay_ms=20)
    await plugin._outbound.send_control(warn_msg, "peer", peer)
    firewall_ctx.send_fire_forget.assert_called_once_with(peer, warn_msg)

@pytest.mark.asyncio
async def test_do_send_bypasses_on_outbound_hook(firewall_ctx):
    """on_outbound sends via ctx.send_to_peer (hook-bypassing) and suppresses normal send."""
    plugin = FirewallPlugin(firewall_ctx, {})
    peer = NodeInfo(node_id="peer", host="1.1.1.1", port=1)
    msg = Announce(node_id="me", skill_key="s1")

    result = await plugin.on_outbound(msg, peer)

    assert result is False  # Suppress normal send path (already sent via fire-forget)
    firewall_ctx.send_fire_forget.assert_called_once_with(peer, msg)

@pytest.mark.asyncio
async def test_warn_delay_0_clears_warn_list(firewall_ctx):
    plugin = FirewallPlugin(firewall_ctx, {})
    plugin._outbound.set_warn("p1", delay_ms=100)
    assert "p1" in plugin._outbound._warn_list
    
    # Send Warn with 0
    await plugin.on_inbound(Warn(delay_ms=0), "1.1.1.1")
    # Note: _get_cert_id won't find cert_id for Warn unless it has public_key (derived to node_id).
    # Let's use a Warn with public_key that derives to p1
    # For test purpose, we can mock _get_cert_id or just set it in msg.
    # Wait, _get_cert_id uses msg.node_id if present.
    # Our Warn inherits Message which has public_key but not node_id.
    # Let's use TaskRequest for easy cert_id if needed, but Warn is a control msg.
    # Actually, we can just mock _get_cert_id for this test or use a message that has node_id.
    with patch.object(plugin, "_get_cert_id", return_value="p1"):
        await plugin.on_inbound(Warn(delay_ms=0), "1.1.1.1")
    assert "p1" not in plugin._outbound._warn_list

@pytest.mark.asyncio
async def test_task_messages_get_qos_priority(firewall_ctx):
    plugin = FirewallPlugin(firewall_ctx, {"pending_queue_size": 100})
    plugin._processing_enabled = False

    # 1. Add normal message
    await plugin.on_inbound(Announce(node_id="p1", skill_key="s1"), "1.1.1.1")
    # 2. Add fire-and-forget task message (TaskResult gets queued with priority)
    await plugin.on_inbound(TaskResult(task_id="t1"), "1.1.1.1")

    key_task = plugin._get_dedup_key(TaskResult(task_id="t1"), None, "1.1.1.1")
    assert plugin._pending[key_task].priority == 1

@pytest.mark.asyncio
async def test_sync_request_rate_limited_by_ip_only(firewall_ctx):
    config = {"base_limit": 2, "window_seconds": 60}
    plugin = FirewallPlugin(firewall_ctx, config)
    plugin._processing_enabled = False
    
    # SyncRequest has no node_id in Phase 1
    msg = SyncRequest()
    await plugin.on_inbound(msg, "1.1.1.1")
    await plugin.on_inbound(msg, "1.1.1.1")
    assert await plugin.on_inbound(msg, "1.1.1.1") is False
    assert "1.1.1.1" in plugin._ip_blocklist

@pytest.mark.asyncio
async def test_adaptive_limit_relaxes_with_empty_queue(firewall_ctx):
    plugin = FirewallPlugin(firewall_ctx, {"base_limit": 100})
    plugin._processing_enabled = False
    # Initial pressure is 0, multiplier is 1.0
    assert plugin._get_effective_limit(Announce(), None) == 100

@pytest.mark.asyncio
async def test_adaptive_limit_tightens_with_full_queue(firewall_ctx):
    plugin = FirewallPlugin(firewall_ctx, {"base_limit": 100, "pending_queue_size": 10})
    plugin._processing_enabled = False
    # Fill queue
    for i in range(10):
        await plugin.on_inbound(Announce(node_id=f"n{i}"), "1.1.1.1")
    
    # Trigger tick multiple times to update EMA and multiplier
    for _ in range(10):
        await plugin.on_tick([], _healthy())
    
    # EMA should now be ~1.0. fill is 1.0 >= 0.85 -> mult = 0.1
    assert plugin._get_effective_limit(Announce(), None) == 10

@pytest.mark.asyncio
async def test_ema_smoothing_dampens_spikes(firewall_ctx):
    plugin = FirewallPlugin(firewall_ctx, {"base_limit": 100, "pending_queue_size": 10})
    plugin._processing_enabled = False
    # One spike to 50%
    for i in range(5):
        await plugin.on_inbound(Announce(node_id=f"n{i}"), "1.1.1.1")
    
    await plugin.on_tick([], _healthy())
    # actual_fill = 0.5. EMA = 0.8*0 + 0.2*0.5 = 0.1
    # 0.1 < 0.3 -> mult remains 1.0
    assert plugin._current_pressure_multiplier == 1.0

@pytest.mark.asyncio
async def test_staleness_discards_old_items(firewall_ctx):
    plugin = FirewallPlugin(firewall_ctx, {"staleness_seconds": 0.1})
    plugin._processing_enabled = False # Keep it queued
    await plugin.on_inbound(Announce(node_id="p1"), "1.1.1.1")
    
    await asyncio.sleep(0.2) # Item is now stale in the queue
    
    plugin._processing_enabled = True # Start processing
    # The process loop should discard it
    await asyncio.sleep(0.1)
    firewall_ctx.delivery_cb.assert_not_called()
    assert len(plugin._pending) == 0

@pytest.mark.asyncio
async def test_request_response_types_pass_through(firewall_ctx):
    """Request/response types return True (node processes synchronously on TCP)."""
    plugin = FirewallPlugin(firewall_ctx, {"pending_queue_size": 100})
    plugin._processing_enabled = False

    assert await plugin.on_inbound(JoinRequest(node_id="n1"), "1.1.1.1") is True
    assert await plugin.on_inbound(Query(value="test"), "2.2.2.2") is True
    assert await plugin.on_inbound(SyncRequest(), "3.3.3.3") is True
    assert await plugin.on_inbound(TaskRequest(task_id="t1"), "4.4.4.4") is True

    # None of these should be in the pending queue
    assert len(plugin._pending) == 0

# ---- ADDITIONAL V2 TESTS ----

@pytest.mark.asyncio
async def test_weighted_rate_limit(firewall_ctx):
    # SyncResponse with 100 skills should count as 100
    config = {"base_limit": 50, "window_seconds": 60}
    plugin = FirewallPlugin(firewall_ctx, config)
    plugin._processing_enabled = False
    
    msg = SyncResponse(skills=[{}] * 100)
    # Should trigger ban immediately as 100 > 50
    assert await plugin.on_inbound(msg, "1.1.1.1") is False
    assert "1.1.1.1" in plugin._ip_blocklist

@pytest.mark.asyncio
async def test_outbound_penalty_queue_drain(firewall_ctx):
    plugin = FirewallPlugin(firewall_ctx, {})
    plugin._processing_enabled = False
    peer = NodeInfo(node_id="p1", host="1.1.1.1", port=1)
    plugin._outbound.set_warn("p1", delay_ms=50)
    
    # 1. Send first message (immediate)
    await plugin.on_outbound(Announce(node_id="me", skill_key="s1"), peer)
    firewall_ctx.send_fire_forget.assert_called_once()
    firewall_ctx.send_fire_forget.reset_mock()

    # 2. Send second message (too soon -> penalty queue)
    await plugin.on_outbound(Announce(node_id="me", skill_key="s2"), peer)
    assert len(plugin._outbound._penalty_queue) == 1
    firewall_ctx.send_fire_forget.assert_not_called()

    # 3. Wait and tick
    await asyncio.sleep(0.06)
    await plugin.on_tick([peer], _healthy())
    assert len(plugin._outbound._penalty_queue) == 0
    firewall_ctx.send_fire_forget.assert_called_once()

@pytest.mark.asyncio
async def test_asymmetric_relaxation(firewall_ctx):
    plugin = FirewallPlugin(firewall_ctx, {"base_limit": 100, "pending_queue_size": 10})
    plugin._processing_enabled = False
    # Tighten to 0.1
    for i in range(10): await plugin.on_inbound(Announce(node_id=f"n{i}"), "1.1.1.1")
    for _ in range(10):
        await plugin.on_tick([], _healthy())
    assert plugin._current_pressure_multiplier == 0.1
    
    # Drain queue
    plugin._pending.clear()
    
    # Tick to relax. Should go 0.1 -> 0.2 (max +0.1 per tick)
    # Need to wait for hysteresis (5s) or bypass it
    plugin._last_band_change = time.monotonic() - 10
    await plugin.on_tick([], _healthy())
    assert round(plugin._current_pressure_multiplier, 1) == 0.2
