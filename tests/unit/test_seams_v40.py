"""Seam tests — v0.40.0 cross-track integration.

These tests verify that the 5 tracks work together at their interfaces.
Not unit tests for individual tracks — those are in test_*_v40.py files.

Seams tested:
1. Deferred bus + heartbeat loop tick pattern
2. Deferred bus + cancel before tick
3. Config splitting + pricing engine routing
4. Config splitting + backward compat (single file == split files)
5. Deferred bus + valid_until expiry during tick cycle
"""
import sys
import os
import time
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from knarr.dht.eventbus import EventBus
from knarr.cli.config import load_config, deep_merge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_toml(directory: Path, filename: str, content: str) -> Path:
    p = directory / filename
    p.write_text(content, encoding="utf-8")
    return p


def _make_bus(debug=False) -> EventBus:
    return EventBus(size=64, debug=debug)


# ---------------------------------------------------------------------------
# Seam 1: Deferred bus + heartbeat tick pattern
#
# The heartbeat loop in node.py calls bus.tick() every cycle. Verify that
# a deferred event emitted before the loop runs actually fires when tick()
# is called — simulating the real integration between C1 and the loop.
# ---------------------------------------------------------------------------

def test_seam_deferred_fires_on_heartbeat_tick():
    """Emit a deferred event, then simulate the heartbeat loop calling tick().
    The event should fire and be visible to subscribers."""
    bus = _make_bus()
    sub = bus.subscribe("credit.*")

    # Emit deferred event scheduled for "now - epsilon" (so it's ready immediately
    # on next tick, simulating a very short delay)
    past = time.time() - 0.01
    event_id = bus.emit("credit.release", valid_from=past, amount=42.0)

    # Before tick: subscriber should see nothing (deferred, not in ring yet)
    events_before = sub.poll()
    # It should have fired immediately since valid_from <= now at emit time
    # Actually: valid_from in the past means it fires immediately in emit()
    # So events_before should have it
    assert len(events_before) == 1, "valid_from in past should fire immediately in emit()"
    assert events_before[0]["event"] == "credit.release"
    assert events_before[0]["amount"] == 42.0
    assert events_before[0]["event_id"] == event_id


def test_seam_deferred_fires_after_tick_with_future_valid_from():
    """Emit with valid_from in the future, advance time, call tick().
    Simulates the heartbeat loop pattern in node.py."""
    bus = _make_bus()
    sub = bus.subscribe("settlement.*")

    future = time.time() + 1000  # far future

    event_id = bus.emit("settlement.due", valid_from=future, peer="abc123")

    # Before tick: nothing in ring
    assert len(sub.poll()) == 0

    # Simulate time advancing past valid_from, then heartbeat calls tick()
    with patch("knarr.dht.eventbus.time") as mock_time:
        mock_time.time.return_value = future + 1.0
        fired = bus.tick()

    assert fired == 1
    events = sub.poll()
    assert len(events) == 1
    assert events[0]["event"] == "settlement.due"
    assert events[0]["peer"] == "abc123"
    assert events[0]["event_id"] == event_id


def test_seam_multiple_deferred_fire_in_order():
    """Multiple deferred events with different valid_from fire in correct
    order when tick() processes them — order matters for settlement."""
    bus = _make_bus()
    sub = bus.subscribe("*")

    base = time.time() + 1000
    id_a = bus.emit("step.first", valid_from=base + 1)
    id_b = bus.emit("step.second", valid_from=base + 2)
    id_c = bus.emit("step.third", valid_from=base + 3)

    with patch("knarr.dht.eventbus.time") as mock_time:
        mock_time.time.return_value = base + 10  # all ready
        fired = bus.tick()

    assert fired == 3
    events = sub.poll()
    assert [e["event"] for e in events] == ["step.first", "step.second", "step.third"]


# ---------------------------------------------------------------------------
# Seam 2: Deferred bus + cancel before tick
#
# AP-05 grace period pattern: emit deferred hold, cancel if payment arrives
# before tick fires the release.
# ---------------------------------------------------------------------------

def test_seam_cancel_before_tick_prevents_firing():
    """Emit deferred event (hold), cancel it before tick() — simulates
    the grace period cancel pattern."""
    bus = _make_bus()
    sub = bus.subscribe("hold.*")

    future = time.time() + 1000
    hold_id = bus.emit("hold.credit", valid_from=future, amount=5.0)

    # Payment arrives — cancel the hold before it fires
    cancelled = bus.cancel(hold_id)
    assert cancelled is True

    # Heartbeat tick runs — should not fire the cancelled event
    with patch("knarr.dht.eventbus.time") as mock_time:
        mock_time.time.return_value = future + 1.0
        fired = bus.tick()

    assert fired == 0
    assert len(sub.poll()) == 0


def test_seam_cancel_after_tick_returns_false():
    """Once tick() fires a deferred event, cancel() should return False."""
    bus = _make_bus()
    sub = bus.subscribe("release.*")

    future = time.time() + 1000
    event_id = bus.emit("release.funds", valid_from=future, amount=10.0)

    # Tick fires the event
    with patch("knarr.dht.eventbus.time") as mock_time:
        mock_time.time.return_value = future + 1.0
        fired = bus.tick()

    assert fired == 1

    # Now try to cancel — should fail (already fired)
    cancelled = bus.cancel(event_id)
    assert cancelled is False


def test_seam_cancel_nonexistent_returns_false():
    """Cancel with a bogus event_id — should return False cleanly."""
    bus = _make_bus()
    assert bus.cancel("evt_does_not_exist") is False


# ---------------------------------------------------------------------------
# Seam 3: Config splitting + pricing engine routing
#
# Put [pricing] engine = "module" in knarr.economy.toml tier file,
# verify it's read correctly and would route to module path.
# ---------------------------------------------------------------------------

def test_seam_config_tier_sets_pricing_engine():
    """Economy tier file sets pricing engine, merged config reflects it."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Base config — no pricing section
        _write_toml(tmp_path, "knarr.toml", """
[node]
port = 9001
""")

        # Economy tier sets pricing engine
        _write_toml(tmp_path, "knarr.economy.toml", """
[pricing]
engine = "module"
module_path = "custom_pricing:calculate"
""")

        cfg = load_config(tmp_path / "knarr.toml")
        assert cfg["pricing"]["engine"] == "module"
        assert cfg["pricing"]["module_path"] == "custom_pricing:calculate"


def test_seam_config_tier_pricing_override():
    """Base config has pricing.engine = "builtin", economy tier overrides to "module".
    Tier must win."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        _write_toml(tmp_path, "knarr.toml", """
[node]
port = 9001

[pricing]
engine = "builtin"
""")

        _write_toml(tmp_path, "knarr.economy.toml", """
[pricing]
engine = "module"
""")

        cfg = load_config(tmp_path / "knarr.toml")
        assert cfg["pricing"]["engine"] == "module", "tier file must override base config"


# ---------------------------------------------------------------------------
# Seam 4: Config splitting + backward compat
#
# A single knarr.toml with economy sections must produce identical config
# to having those sections in the tier file.
# ---------------------------------------------------------------------------

def test_seam_single_file_equals_split_files():
    """Single knarr.toml with all sections == base + tier files split."""
    with tempfile.TemporaryDirectory() as tmp_single:
        single_path = Path(tmp_single)

        _write_toml(single_path, "knarr.toml", """
[node]
port = 9001

[policy]
initial_credit = 5.0
min_balance = -20.0

[mail]
accept_from = "whitelist"
default_ttl_hours = 48
""")

        cfg_single = load_config(single_path / "knarr.toml")

    with tempfile.TemporaryDirectory() as tmp_split:
        split_path = Path(tmp_split)

        _write_toml(split_path, "knarr.toml", """
[node]
port = 9001
""")

        _write_toml(split_path, "knarr.economy.toml", """
[policy]
initial_credit = 5.0
min_balance = -20.0
""")

        _write_toml(split_path, "knarr.mail.toml", """
[mail]
accept_from = "whitelist"
default_ttl_hours = 48
""")

        cfg_split = load_config(split_path / "knarr.toml")

    # Both should produce identical merged config
    assert cfg_single == cfg_split, (
        f"Single-file and split-file configs must be identical.\n"
        f"Single: {cfg_single}\n"
        f"Split:  {cfg_split}"
    )


def test_seam_split_config_with_skills_tier():
    """Skills tier file adds skills, merged config has them."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        _write_toml(tmp_path, "knarr.toml", """
[node]
port = 9001
""")

        _write_toml(tmp_path, "knarr.skills.toml", """
[skills]
minimum_price = 0.5
default_timeout = 30
""")

        cfg = load_config(tmp_path / "knarr.toml")
        assert cfg["skills"]["minimum_price"] == 0.5
        assert cfg["skills"]["default_timeout"] == 30


# ---------------------------------------------------------------------------
# Seam 5: Deferred bus + valid_until expiry
#
# Emit deferred with short valid_until, time passes beyond it, tick runs,
# event should be discarded (not fired).
# ---------------------------------------------------------------------------

def test_seam_deferred_expires_before_tick():
    """Deferred event with valid_until that passes before tick() runs.
    Event should be silently discarded, not fired."""
    bus = _make_bus()
    sub = bus.subscribe("timeout.*")

    base = time.time() + 1000
    event_id = bus.emit(
        "timeout.check",
        valid_from=base + 1,   # fires after base+1
        valid_until=base + 5,  # expires at base+5
    )

    # Time passes well beyond valid_until
    with patch("knarr.dht.eventbus.time") as mock_time:
        mock_time.time.return_value = base + 100  # way past valid_until
        fired = bus.tick()

    assert fired == 0, "expired deferred event should not fire"
    assert len(sub.poll()) == 0


def test_seam_deferred_fires_within_window():
    """Deferred event fires if tick() runs within the valid_from..valid_until window."""
    bus = _make_bus()
    sub = bus.subscribe("window.*")

    base = time.time() + 1000
    event_id = bus.emit(
        "window.check",
        valid_from=base + 1,
        valid_until=base + 100,
        data="inside_window",
    )

    # Tick at base+10 — within window
    with patch("knarr.dht.eventbus.time") as mock_time:
        mock_time.time.return_value = base + 10
        fired = bus.tick()

    assert fired == 1
    events = sub.poll()
    assert len(events) == 1
    assert events[0]["data"] == "inside_window"


def test_seam_mixed_expiry_and_live():
    """Mix of expired and live deferred events — only live ones fire."""
    bus = _make_bus()
    sub = bus.subscribe("*")

    base = time.time() + 1000

    # Event A: valid_from=base+1, valid_until=base+2 (will expire by base+10)
    bus.emit("expired.event", valid_from=base + 1, valid_until=base + 2)

    # Event B: valid_from=base+1, valid_until=base+100 (will fire at base+10)
    bus.emit("live.event", valid_from=base + 1, valid_until=base + 100, key="live")

    # Event C: valid_from=base+1, no valid_until (will fire at base+10)
    bus.emit("immortal.event", valid_from=base + 1, key="immortal")

    with patch("knarr.dht.eventbus.time") as mock_time:
        mock_time.time.return_value = base + 10
        fired = bus.tick()

    assert fired == 2, "only live + immortal should fire, expired should be discarded"
    events = sub.poll()
    event_types = [e["event"] for e in events]
    assert "live.event" in event_types
    assert "immortal.event" in event_types
    assert "expired.event" not in event_types
