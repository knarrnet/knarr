"""
M-015: Tick starvation fix.

Root cause: Peer liveness sweep in _heartbeat_tick() has no overall timeout.
If N peers are dead with 8s TCP connect timeout each, tick blocks for N*8 seconds.

Fix: Extract peer sweep into _peer_liveness_sweep(), wrap with asyncio.wait_for(10s).

Integration point: Replace lines 4333-4377 in node.py with call to this helper.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Dict, List

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

PEER_SWEEP_TIMEOUT = 10.0  # Max seconds for entire peer liveness sweep
PER_PEER_HEARTBEAT_TIMEOUT = 2.0  # Max seconds per individual heartbeat send


async def peer_liveness_sweep(
    peers: list,
    peer_last_activity: Dict[str, float],
    heartbeat_silence_threshold: float,
    peer_dead_timeout: float,
    send_heartbeat_fn,
    remove_peer_fn,
    now: float = None,
) -> dict:
    """Check peer liveness with bounded timeout.

    Pure async function: takes callbacks, returns stats.
    Caller passes in node methods as callbacks.

    Args:
        peers: list of peer objects with .node_id, .host, .port
        peer_last_activity: mutable dict of node_id -> last_seen monotonic time
        heartbeat_silence_threshold: seconds of silence before sending heartbeat
        peer_dead_timeout: seconds of silence before removing peer
        send_heartbeat_fn: async (peer) -> bool  (returns True if heartbeat succeeded)
        remove_peer_fn: async (node_id) -> None
        now: current monotonic time (default: time.monotonic())

    Returns:
        dict with keys: checked, alive, dead, timed_out
    """
    if now is None:
        now = time.monotonic()

    stats = {"checked": 0, "alive": 0, "dead": 0, "timed_out": 0}

    for peer in peers:
        last_seen = peer_last_activity.get(peer.node_id)
        if last_seen is None:
            peer_last_activity[peer.node_id] = now
            continue

        silence = now - last_seen
        stats["checked"] += 1

        if silence > peer_dead_timeout:
            logger.warning(
                f"PEER_DEAD id={peer.node_id[:16]} silence={silence:.0f}s"
            )
            try:
                await remove_peer_fn(peer.node_id)
            except Exception:
                logger.exception(f"PEER_REMOVE_ERROR id={peer.node_id[:16]}")
            peer_last_activity.pop(peer.node_id, None)
            stats["dead"] += 1
            continue

        if silence > heartbeat_silence_threshold:
            try:
                alive = await asyncio.wait_for(
                    send_heartbeat_fn(peer),
                    timeout=PER_PEER_HEARTBEAT_TIMEOUT,
                )
                if alive:
                    peer_last_activity[peer.node_id] = time.monotonic()
                    stats["alive"] += 1
                else:
                    stats["timed_out"] += 1
            except asyncio.TimeoutError:
                logger.debug(
                    f"HB_SEND_TIMEOUT to={peer.node_id[:16]} (>{PER_PEER_HEARTBEAT_TIMEOUT}s)"
                )
                stats["timed_out"] += 1
            except Exception:
                logger.exception(f"HB_SEND_ERROR to={peer.node_id[:16]}")
                stats["timed_out"] += 1

    return stats


async def bounded_peer_sweep(
    peers: list,
    peer_last_activity: Dict[str, float],
    heartbeat_silence_threshold: float,
    peer_dead_timeout: float,
    send_heartbeat_fn,
    remove_peer_fn,
) -> dict:
    """Peer liveness sweep with overall timeout guard.

    This is the function that replaces the inline loop in _heartbeat_tick().
    """
    try:
        return await asyncio.wait_for(
            peer_liveness_sweep(
                peers, peer_last_activity,
                heartbeat_silence_threshold, peer_dead_timeout,
                send_heartbeat_fn, remove_peer_fn,
            ),
            timeout=PEER_SWEEP_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"TICK_PEER_CHECK_TIMEOUT: peer liveness sweep exceeded {PEER_SWEEP_TIMEOUT}s"
        )
        return {"checked": 0, "alive": 0, "dead": 0, "timed_out": 0, "sweep_timeout": True}
