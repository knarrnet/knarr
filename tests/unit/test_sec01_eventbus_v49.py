"""SEC-01 tests: EventBus._wake_subscribers thread-safety via call_soon_threadsafe.

Verifies:
1. `_loop` is in EventBus.__slots__
2. `_loop` is captured in __init__ (None outside event loop)
3. `_loop` is set when constructed inside a running loop
4. call_soon_threadsafe is used from a ThreadPoolExecutor thread
5. emit() from a thread correctly wakes an async subscriber
"""
import asyncio
import concurrent.futures
import threading

import pytest


def make_bus(size=64):
    from knarr.dht.eventbus import EventBus
    return EventBus(size=size)


# ── 1. __slots__ check ────────────────────────────────────────────────────────

def test_loop_in_slots():
    from knarr.dht.eventbus import EventBus
    assert "_loop" in EventBus.__slots__, "_loop must be in EventBus.__slots__"


# ── 2. _loop is None outside event loop ──────────────────────────────────────

def test_loop_none_outside_event_loop():
    bus = make_bus()
    assert bus._loop is None, "_loop must be None when constructed outside event loop"


# ── 3. _loop is set when inside running loop ─────────────────────────────────

@pytest.mark.asyncio
async def test_loop_set_inside_event_loop():
    from knarr.dht.eventbus import EventBus
    bus = EventBus()
    assert bus._loop is asyncio.get_running_loop(), "_loop must be the running loop"


# ── 4. call_soon_threadsafe is used from executor thread ─────────────────────

@pytest.mark.asyncio
async def test_wake_uses_call_soon_threadsafe():
    """Patch call_soon_threadsafe to verify it's called from a thread."""
    from knarr.dht.eventbus import EventBus
    bus = EventBus()
    called_from_thread = []
    original_csst = bus._loop.call_soon_threadsafe

    def patched_csst(fn, *args):
        called_from_thread.append(threading.current_thread().name)
        original_csst(fn, *args)

    bus._loop.call_soon_threadsafe = patched_csst

    sub = bus.subscribe("test.event")

    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        await loop.run_in_executor(pool, lambda: bus.emit("test.event", data="x"))

    # Give the event loop a chance to process
    await asyncio.sleep(0.05)
    assert len(called_from_thread) >= 1, "call_soon_threadsafe must be called from thread"
    bus._loop.call_soon_threadsafe = original_csst


# ── 5. emit from thread wakes async subscriber ───────────────────────────────

@pytest.mark.asyncio
async def test_emit_from_thread_wakes_subscriber():
    """End-to-end: emit from ThreadPoolExecutor thread, subscriber receives event."""
    from knarr.dht.eventbus import EventBus
    bus = EventBus()
    sub = bus.subscribe("thread.event")

    loop = asyncio.get_running_loop()
    received = []

    async def consumer():
        evt = await asyncio.wait_for(sub.next(), timeout=2.0)
        received.append(evt)

    consumer_task = asyncio.create_task(consumer())

    # Small delay so consumer reaches await
    await asyncio.sleep(0.01)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        await loop.run_in_executor(pool, lambda: bus.emit("thread.event", value=42))

    await consumer_task
    assert len(received) == 1
    assert received[0]["value"] == 42


# ── 6. emit from same thread also works (fallback path not broken) ────────────

@pytest.mark.asyncio
async def test_emit_from_same_thread_still_works():
    """Direct emit (same thread) still wakes subscriber — fallback path."""
    from knarr.dht.eventbus import EventBus
    bus = EventBus()
    sub = bus.subscribe("direct.event")

    received = []

    async def consumer():
        evt = await asyncio.wait_for(sub.next(), timeout=2.0)
        received.append(evt)

    consumer_task = asyncio.create_task(consumer())
    await asyncio.sleep(0.01)

    bus.emit("direct.event", value="hello")

    await consumer_task
    assert received[0]["value"] == "hello"


# ── 7. No _loop on Subscriber.__slots__ ──────────────────────────────────────

def test_subscriber_slots_no_loop():
    from knarr.dht.eventbus import Subscriber
    assert "_loop" not in Subscriber.__slots__, "_loop must NOT be in Subscriber.__slots__"
