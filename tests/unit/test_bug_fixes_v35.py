"""Tests for Track A bug fixes."""

import asyncio
import io
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys_path_entry = __import__("os").path.join(
    __import__("os").path.dirname(__file__), "..", "..", "src"
)
sys.path.insert(0, sys_path_entry)

from knarr.patches.m015_tick_starvation import (
    PEER_SWEEP_TIMEOUT,
    PER_PEER_HEARTBEAT_TIMEOUT,
    bounded_peer_sweep,
    peer_liveness_sweep,
)
from knarr.patches.o021_config_warnings import warn_unknown_keys


# ── M-015: Tick Starvation ─────────────────────────────────────────────


def _make_peer(node_id, host="127.0.0.1", port=4001):
    return SimpleNamespace(node_id=node_id, host=host, port=port)


class TestPeerLivenessSweep:
    @pytest.mark.asyncio
    async def test_new_peer_initialized(self):
        """New peer (not in activity map) gets initialized, not checked."""
        peer = _make_peer("aaa")
        activity = {}

        async def noop_send(p):
            return True

        async def noop_remove(nid):
            pass

        stats = await peer_liveness_sweep(
            [peer], activity, 90, 300, noop_send, noop_remove, now=1000.0,
        )
        assert activity["aaa"] == 1000.0
        assert stats["checked"] == 0

    @pytest.mark.asyncio
    async def test_dead_peer_removed(self):
        """Peer silent beyond dead_timeout gets removed."""
        peer = _make_peer("dead_peer")
        activity = {"dead_peer": 0.0}
        removed = []

        async def noop_send(p):
            return True

        async def track_remove(nid):
            removed.append(nid)

        stats = await peer_liveness_sweep(
            [peer], activity, 90, 300, noop_send, track_remove, now=500.0,
        )
        assert stats["dead"] == 1
        assert "dead_peer" in removed
        assert "dead_peer" not in activity

    @pytest.mark.asyncio
    async def test_silent_peer_gets_heartbeat(self):
        """Peer beyond silence threshold but not dead gets heartbeat."""
        peer = _make_peer("silent_peer")
        activity = {"silent_peer": 0.0}
        sent_to = []

        async def track_send(p):
            sent_to.append(p.node_id)
            return True

        async def noop_remove(nid):
            pass

        stats = await peer_liveness_sweep(
            [peer], activity, 90, 300, track_send, noop_remove, now=100.0,
        )
        assert "silent_peer" in sent_to
        assert stats["alive"] == 1

    @pytest.mark.asyncio
    async def test_active_peer_skipped(self):
        """Peer within silence threshold is not heartbeated."""
        peer = _make_peer("active_peer")
        activity = {"active_peer": 50.0}
        sent_to = []

        async def track_send(p):
            sent_to.append(p.node_id)
            return True

        async def noop_remove(nid):
            pass

        stats = await peer_liveness_sweep(
            [peer], activity, 90, 300, track_send, noop_remove, now=100.0,
        )
        assert "active_peer" not in sent_to
        assert stats["checked"] == 1
        assert stats["alive"] == 0

    @pytest.mark.asyncio
    async def test_slow_heartbeat_times_out(self):
        """Per-peer heartbeat timeout fires for slow peers."""
        peer = _make_peer("slow_peer")
        activity = {"slow_peer": 0.0}

        async def slow_send(p):
            await asyncio.sleep(10)  # way over PER_PEER_HEARTBEAT_TIMEOUT
            return True

        async def noop_remove(nid):
            pass

        stats = await peer_liveness_sweep(
            [peer], activity, 90, 300, slow_send, noop_remove, now=100.0,
        )
        assert stats["timed_out"] == 1

    @pytest.mark.asyncio
    async def test_failed_heartbeat_counted(self):
        """Heartbeat that returns False counts as timed_out."""
        peer = _make_peer("fail_peer")
        activity = {"fail_peer": 0.0}

        async def fail_send(p):
            return False

        async def noop_remove(nid):
            pass

        stats = await peer_liveness_sweep(
            [peer], activity, 90, 300, fail_send, noop_remove, now=100.0,
        )
        assert stats["timed_out"] == 1


class TestBoundedPeerSweep:
    @pytest.mark.asyncio
    async def test_overall_timeout(self):
        """Sweep that exceeds PEER_SWEEP_TIMEOUT returns timeout marker."""
        now = time.monotonic()
        peers = [_make_peer(f"peer_{i}") for i in range(50)]
        # Put all peers in "silent" zone (between silence_threshold and dead_timeout)
        # so they trigger heartbeat sends (not instant removal)
        activity = {f"peer_{i}": now - 100.0 for i in range(50)}

        async def very_slow_send(p):
            await asyncio.sleep(1)  # 50 peers * 1s each = 50s total
            return True

        async def noop_remove(nid):
            pass

        start = time.monotonic()
        stats = await bounded_peer_sweep(
            peers, activity, 90, 300, very_slow_send, noop_remove,
        )
        elapsed = time.monotonic() - start
        # Should complete in ~PEER_SWEEP_TIMEOUT, not 50s
        assert elapsed < PEER_SWEEP_TIMEOUT + 2.0
        assert stats.get("sweep_timeout") is True


# ── O-021: Config Parser Warnings ──────────────────────────────────────


class TestConfigWarnings:
    def test_warns_on_unknown_scalar(self):
        """Unknown scalar key triggers warning."""
        raw = {"skills": {"minimum_price": 0.01, "typo_key": True}}
        known = {"skills": {"minimum_price", "default_timeout"}}

        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            count = warn_unknown_keys(raw, Path("test.toml"), known)
        finally:
            sys.stderr = old_stderr

        assert count == 1
        assert "typo_key" in captured.getvalue()

    def test_skips_nested_tables(self):
        """Nested tables (per-skill config) should not trigger warnings."""
        raw = {
            "skills": {
                "minimum_price": 0.01,
                "llm-chat": {"price": 1.5, "timeout": 30},
                "echo": {"price": 0.0},
            }
        }
        known = {"skills": {"minimum_price", "default_timeout"}}

        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            count = warn_unknown_keys(raw, Path("test.toml"), known)
        finally:
            sys.stderr = old_stderr

        assert count == 0
        assert captured.getvalue() == ""

    def test_mixed_scalar_and_table(self):
        """Warn on unknown scalar, skip nested table."""
        raw = {
            "skills": {
                "minimum_price": 0.01,
                "llm-chat": {"price": 1.5},
                "bad_typo": 42,
            }
        }
        known = {"skills": {"minimum_price", "default_timeout"}}

        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            count = warn_unknown_keys(raw, Path("test.toml"), known)
        finally:
            sys.stderr = old_stderr

        assert count == 1
        assert "bad_typo" in captured.getvalue()
        assert "llm-chat" not in captured.getvalue()

    def test_known_key_no_warning(self):
        """Known keys produce no warnings."""
        raw = {"skills": {"minimum_price": 0.01, "default_timeout": 30}}
        known = {"skills": {"minimum_price", "default_timeout"}}

        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            count = warn_unknown_keys(raw, Path("test.toml"), known)
        finally:
            sys.stderr = old_stderr

        assert count == 0

    def test_section_not_in_raw(self):
        """Missing section in raw config is fine."""
        raw = {"node": {"port": 4001}}
        known = {"skills": {"minimum_price"}}

        count = warn_unknown_keys(raw, Path("test.toml"), known)
        assert count == 0

    def test_policy_nested_tables(self):
        """Policy section with peer-specific sub-tables."""
        raw = {
            "policy": {
                "initial_credit": 3.0,
                "tit_for_tat": False,
                "skill": {"llm-chat": {"min_balance": -2.0}},
                "group": {"felag_alpha": {"credit_limit": 50.0}},
            }
        }
        known = {"policy": {"initial_credit", "min_balance", "tit_for_tat", "group", "skill"}}

        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            count = warn_unknown_keys(raw, Path("test.toml"), known)
        finally:
            sys.stderr = old_stderr

        assert count == 0
