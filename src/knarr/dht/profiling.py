"""F-01: Startup memory profiling with tracemalloc.

Enabled by: KNARR_TRACEMALLOC=1 environment variable.

Call take_snapshot(label) at key startup checkpoints; call print_report() to
emit the top allocators to stderr when startup is complete.
"""

import logging
import os

logger = logging.getLogger(__name__)

_ENABLED: bool = os.environ.get("KNARR_TRACEMALLOC", "0").strip() in ("1", "true", "yes")
_snapshots: list = []


def is_enabled() -> bool:
    return _ENABLED


def start() -> None:
    """Start tracemalloc if enabled. Called once at process startup."""
    if not _ENABLED:
        return
    import tracemalloc
    if not tracemalloc.is_tracing():
        tracemalloc.start()
    logger.debug("TRACEMALLOC_STARTED env=KNARR_TRACEMALLOC=1")


def take_snapshot(label: str) -> None:
    """Take a named memory snapshot at a startup checkpoint.

    Labels used (in order):
    - storage_init
    - plugin_load
    - join
    - register_system_skills
    - first_tick
    """
    if not _ENABLED:
        return
    import tracemalloc
    if not tracemalloc.is_tracing():
        return
    snap = tracemalloc.take_snapshot()
    _snapshots.append((label, snap))
    logger.debug("TRACEMALLOC_SNAPSHOT label=%s count=%d", label, len(_snapshots))


def print_report(top_n: int = 10) -> None:
    """Print a memory growth report to stderr.

    Shows top allocators between each consecutive snapshot pair plus the
    cumulative top allocators from start to finish.
    """
    if not _ENABLED or not _snapshots:
        return
    import tracemalloc
    import sys

    print("\n=== KNARR STARTUP MEMORY PROFILE ===", file=sys.stderr)
    print(f"Snapshots taken: {[s[0] for s in _snapshots]}", file=sys.stderr)

    if len(_snapshots) >= 2:
        for i in range(1, len(_snapshots)):
            prev_label, prev_snap = _snapshots[i - 1]
            cur_label, cur_snap = _snapshots[i]
            stats = cur_snap.compare_to(prev_snap, "lineno")
            print(f"\n--- Growth: {prev_label} -> {cur_label} ---", file=sys.stderr)
            for stat in stats[:top_n]:
                print(f"  {stat}", file=sys.stderr)

    # Cumulative: first to last
    if len(_snapshots) >= 2:
        first_label, first_snap = _snapshots[0]
        last_label, last_snap = _snapshots[-1]
        stats = last_snap.compare_to(first_snap, "lineno")
        print(f"\n--- Cumulative: {first_label} -> {last_label} (top {top_n}) ---", file=sys.stderr)
        for stat in stats[:top_n]:
            print(f"  {stat}", file=sys.stderr)

    print("=== END MEMORY PROFILE ===\n", file=sys.stderr)
