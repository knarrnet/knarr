"""F-01: Startup memory profiling with tracemalloc.

Tests verify:
- profiling module exists and exports expected functions
- take_snapshot() only acts when KNARR_TRACEMALLOC=1
- print_report() produces output when snapshots are present
- DHTNode __init__ calls _profiling.start() and take_snapshot("storage_init")
- No crash when profiling is disabled (the default)
"""

import importlib
import os
import sys
import pytest
from unittest.mock import patch, MagicMock


# ──────────────────────────────────────────────────────────────────────────────
# F-01-A: profiling module exists with expected API
# ──────────────────────────────────────────────────────────────────────────────

def test_profiling_module_exists():
    """knarr.dht.profiling must exist and export start/take_snapshot/print_report."""
    from knarr.dht import profiling
    assert callable(profiling.start)
    assert callable(profiling.take_snapshot)
    assert callable(profiling.print_report)
    assert callable(profiling.is_enabled)


# ──────────────────────────────────────────────────────────────────────────────
# F-01-B: profiling disabled by default — no-ops
# ──────────────────────────────────────────────────────────────────────────────

def test_profiling_disabled_by_default(monkeypatch):
    """When KNARR_TRACEMALLOC is not set, profiling is a no-op."""
    monkeypatch.delenv("KNARR_TRACEMALLOC", raising=False)

    # Re-import to get fresh module state
    import knarr.dht.profiling as prof

    # No snapshots in disabled state — take_snapshot does nothing
    original_snaps = list(prof._snapshots)
    prof.take_snapshot("test-label")
    assert len(prof._snapshots) == len(original_snaps), (
        "take_snapshot() must not record when disabled"
    )


# ──────────────────────────────────────────────────────────────────────────────
# F-01-C: take_snapshot records when enabled
# ──────────────────────────────────────────────────────────────────────────────

def test_take_snapshot_records_when_enabled():
    """When profiling is enabled, take_snapshot records a labelled snapshot."""
    import tracemalloc
    import knarr.dht.profiling as prof

    # Temporarily force-enable
    original_enabled = prof._ENABLED
    original_snaps = prof._snapshots[:]
    prof._ENABLED = True
    prof._snapshots.clear()

    try:
        tracemalloc.start()
        prof.take_snapshot("test-checkpoint")
        assert len(prof._snapshots) == 1
        assert prof._snapshots[0][0] == "test-checkpoint"
    finally:
        prof._ENABLED = original_enabled
        prof._snapshots.clear()
        prof._snapshots.extend(original_snaps)
        tracemalloc.stop()


# ──────────────────────────────────────────────────────────────────────────────
# F-01-D: print_report produces output to stderr
# ──────────────────────────────────────────────────────────────────────────────

def test_print_report_outputs_to_stderr(capsys):
    """print_report must write to stderr and include section header."""
    import tracemalloc
    import knarr.dht.profiling as prof

    original_enabled = prof._ENABLED
    original_snaps = prof._snapshots[:]
    prof._ENABLED = True
    prof._snapshots.clear()

    try:
        tracemalloc.start()
        prof.take_snapshot("snap-a")
        # Allocate a little memory between snapshots
        _ = [0] * 1000
        prof.take_snapshot("snap-b")
        prof.print_report(top_n=3)
    finally:
        prof._ENABLED = original_enabled
        prof._snapshots.clear()
        prof._snapshots.extend(original_snaps)
        tracemalloc.stop()

    captured = capsys.readouterr()
    assert "KNARR STARTUP MEMORY PROFILE" in captured.err
    assert "snap-a" in captured.err
    assert "snap-b" in captured.err


# ──────────────────────────────────────────────────────────────────────────────
# F-01-E: DHTNode __init__ calls _profiling.start() and take_snapshot
# ──────────────────────────────────────────────────────────────────────────────

def test_dhtnode_init_calls_profiling():
    """DHTNode.__init__ must call _profiling.start() and take_snapshot('storage_init')."""
    import knarr.dht.profiling as prof

    calls = []
    original_start = prof.start
    original_snap = prof.take_snapshot

    def mock_start():
        calls.append("start")

    def mock_snap(label):
        calls.append(("snapshot", label))

    prof.start = mock_start
    prof.take_snapshot = mock_snap

    try:
        from knarr.dht.node import DHTNode
        node = DHTNode(storage_path=":memory:", config={})
    finally:
        prof.start = original_start
        prof.take_snapshot = original_snap

    assert "start" in calls, "DHTNode.__init__ must call _profiling.start()"
    assert ("snapshot", "storage_init") in calls, (
        "DHTNode.__init__ must call _profiling.take_snapshot('storage_init')"
    )


# ──────────────────────────────────────────────────────────────────────────────
# F-01-F: print_report is silent when no snapshots
# ──────────────────────────────────────────────────────────────────────────────

def test_print_report_silent_with_no_snapshots(capsys):
    """print_report must produce no output when snapshots list is empty."""
    import knarr.dht.profiling as prof

    original_enabled = prof._ENABLED
    original_snaps = prof._snapshots[:]
    prof._ENABLED = True
    prof._snapshots.clear()

    try:
        prof.print_report()
    finally:
        prof._ENABLED = original_enabled
        prof._snapshots.clear()
        prof._snapshots.extend(original_snaps)

    captured = capsys.readouterr()
    assert captured.err == ""
