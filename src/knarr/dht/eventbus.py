"""EventBus: fixed-size ring buffer with topic subscriptions.

The bus promises nothing. Events are volatile — ring buffer, oldest evicted,
no replay. Receipts are durable; the bus is a nervous system, not a memory.

Design:
- emit() is synchronous: O(1) write + O(subs) wake-set. Never blocks.
- next() is async: blocks until a matching event is available.
- poll() is synchronous: returns all pending matches without blocking.
- Topic matching via fnmatch: "receipt.*" matches "receipt.issued".
- Slow subscribers skip to oldest available; they never crash the bus.
"""
import asyncio
import fnmatch
import threading
import time
import logging

log = logging.getLogger(__name__)


class EventBus:
    """Fixed-size ring buffer with topic subscriptions.

    The bus promises nothing. Events are volatile. Oldest evicted when full.
    Subscribers read at their own pace; if they fall behind, they skip.
    """

    __slots__ = ("_ring", "_size", "_head", "_subs", "_debug", "_lock")

    def __init__(self, size: int = 256, debug: bool = False):
        size = max(16, min(65536, size))  # clamp to sane range
        self._ring = [None] * size
        self._size = size
        self._head = 0
        self._subs: list["Subscriber"] = []
        self._debug = debug
        self._lock = threading.Lock()  # thread-safe emit from handler pool

    def subscribe(self, *patterns: str) -> "Subscriber":
        """Create a subscriber for events matching any of the given glob patterns.

        Examples: 'receipt.issued', 'receipt.*', 'log.error.*'
        """
        sub = Subscriber(self, patterns, cursor=self._head)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: "Subscriber"):
        """Remove a subscriber."""
        self._subs = [s for s in self._subs if s is not sub]

    def emit(self, event_type: str, **fields):
        """Write event to ring buffer, wake matching subscribers.

        Synchronous — never blocks, never awaits. Thread-safe via lock.
        Receipt before bus: callers must persist receipt BEFORE calling emit().
        """
        event = {"event": event_type, "ts": time.time(), **fields}
        with self._lock:
            self._ring[self._head % self._size] = event
            self._head += 1
        if self._debug:
            log.debug(f"EVENT {event_type}: {fields}")
        for sub in self._subs:
            if sub._matches(event_type):
                try:
                    sub._wake.set()
                except Exception:
                    pass  # dead subscriber — cleaned up on next unsubscribe


class Subscriber:
    """Cursor into the ring buffer. Reads at own pace, skips if behind."""

    __slots__ = ("_bus", "_patterns", "_cursor", "_wake")

    def __init__(self, bus: EventBus, patterns: tuple, cursor: int):
        self._bus = bus
        self._patterns = patterns
        self._cursor = cursor
        self._wake = asyncio.Event()

    def _matches(self, event_type: str) -> bool:
        for p in self._patterns:
            if p == event_type or fnmatch.fnmatch(event_type, p):
                return True
        return False

    async def next(self) -> dict:
        """Block until the next matching event. Skip if behind ring."""
        while True:
            oldest = self._bus._head - self._bus._size
            if self._cursor < oldest:
                self._cursor = oldest

            while self._cursor < self._bus._head:
                idx = self._cursor % self._bus._size
                event = self._bus._ring[idx]
                self._cursor += 1
                if event and self._matches(event["event"]):
                    return event

            self._wake.clear()
            await self._wake.wait()

    def poll(self) -> list:
        """Non-blocking: return all matching events since last read."""
        oldest = self._bus._head - self._bus._size
        if self._cursor < oldest:
            self._cursor = oldest
        results = []
        while self._cursor < self._bus._head:
            idx = self._cursor % self._bus._size
            event = self._bus._ring[idx]
            self._cursor += 1
            if event and self._matches(event["event"]):
                results.append(event)
        self._wake.clear()
        return results
