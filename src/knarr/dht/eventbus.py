"""EventBus: fixed-size ring buffer with topic subscriptions.

The bus promises nothing. Events are volatile — ring buffer, oldest evicted,
no replay. Receipts are durable; the bus is a nervous system, not a memory.

Design:
- emit() is synchronous: O(1) write + O(subs) wake-set. Never blocks.
- next() is async: blocks until a matching event is available.
- poll() is synchronous: returns all pending matches without blocking.
- Topic matching via fnmatch: "receipt.*" matches "receipt.issued".
- Slow subscribers skip to oldest available; they never crash the bus.

v0.40.0 additions (deferred bus primitive):
- emit() now accepts optional valid_from and valid_until parameters.
- emit() now returns an event_id str (always, for both immediate and deferred).
- Deferred event store: in-memory list sorted by valid_from ascending.
- tick(): process deferred events — call once per heartbeat cycle.
- cancel(): remove a deferred event before it fires.
"""
import asyncio
import fnmatch
import math
import secrets
import threading
import time
import logging

log = logging.getLogger(__name__)


class EventBus:
    """Fixed-size ring buffer with topic subscriptions.

    The bus promises nothing. Events are volatile. Oldest evicted when full.
    Subscribers read at their own pace; if they fall behind, they skip.

    v0.40.0: Deferred events can be scheduled for the future via valid_from.
    Events with valid_until in the past at emit time are silently discarded.
    """

    __slots__ = ("_ring", "_size", "_head", "_subs", "_debug", "_lock", "_deferred")

    def __init__(self, size: int = 256, debug: bool = False):
        size = max(16, min(65536, size))  # clamp to sane range
        self._ring = [None] * size
        self._size = size
        self._head = 0
        self._subs: list["Subscriber"] = []
        self._debug = debug
        self._lock = threading.Lock()  # thread-safe emit from handler pool
        # v0.40.0: deferred store — list of entry dicts sorted by valid_from asc.
        # Protected by the same _lock as ring buffer writes.
        self._deferred: list[dict] = []

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

    def emit(self, event_type: str, valid_from: float = None,
             valid_until: float = None, **fields) -> str:
        """Write event to ring buffer, wake matching subscribers.

        Synchronous — never blocks, never awaits. Thread-safe via lock.
        Receipt before bus: callers must persist receipt BEFORE calling emit().

        v0.40.0 extensions:
        - valid_from: if set and in the future, store in deferred queue.
          If None or <= now, fires immediately (backward compatible).
        - valid_until: if set and already past at emit time, silently discard.
          If both set and valid_from >= valid_until, log warning and discard.
        - Returns event_id (str) — always, even for immediate events.
          Callers that ignore the return value are unaffected.
        """
        event_id = f"evt_{secrets.token_hex(8)}"
        now = time.time()

        # Reject non-finite timestamps (NaN/Inf bypass comparison guards)
        if valid_from is not None and not math.isfinite(valid_from):
            log.warning(f"EMIT_INVALID valid_from={valid_from} — must be finite")
            return event_id
        if valid_until is not None and not math.isfinite(valid_until):
            log.warning(f"EMIT_INVALID valid_until={valid_until} — must be finite")
            return event_id

        # Silently discard if valid_until is already in the past
        if valid_until is not None and valid_until < now:
            if self._debug:
                log.info(
                    f"EMIT_EXPIRED event_type={event_type} event_id={event_id} "
                    f"valid_until={valid_until}"
                )
            return event_id

        # Discard with warning if valid_from >= valid_until (invalid range)
        if valid_from is not None and valid_until is not None and valid_from >= valid_until:
            log.warning(
                f"EMIT_INVALID_RANGE event_type={event_type} event_id={event_id} "
                f"valid_from={valid_from} valid_until={valid_until} "
                f"(valid_from >= valid_until — discarding)"
            )
            return event_id

        event = {
            "event": event_type,
            "event_id": event_id,
            "ts": now,
            "valid_from": valid_from,
            "valid_until": valid_until,
            **fields,
        }

        # Defer if valid_from is strictly in the future
        if valid_from is not None and valid_from > now:
            entry = {
                "event_id": event_id,
                "event": event,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "created_at": now,
            }
            with self._lock:
                # Insert in sorted position by valid_from ascending
                inserted = False
                for i, existing in enumerate(self._deferred):
                    if valid_from < existing["valid_from"]:
                        self._deferred.insert(i, entry)
                        inserted = True
                        break
                if not inserted:
                    self._deferred.append(entry)
            if self._debug:
                log.info(
                    f"EMIT_DEFERRED event_type={event_type} event_id={event_id} "
                    f"valid_from={valid_from}"
                )
            return event_id

        # Immediate fire path
        self._write_to_ring(event)
        if self._debug:
            log.info(f"EVENT {event_type}: event_id={event_id}")
        self._wake_subscribers(event_type)
        return event_id

    def _write_to_ring(self, event: dict) -> None:
        """Write event into the ring buffer under lock."""
        with self._lock:
            self._ring[self._head % self._size] = event
            self._head += 1

    def _wake_subscribers(self, event_type: str) -> None:
        """Wake all subscribers matching event_type."""
        for sub in self._subs:
            if sub._matches(event_type):
                try:
                    sub._wake.set()
                except Exception:
                    pass  # dead subscriber — cleaned up on next unsubscribe

    def tick(self) -> int:
        """Process deferred events. Called once per heartbeat cycle.

        1. Discard events whose valid_until < now (expired before firing).
        2. Fire events whose valid_from <= now, in valid_from order.
        3. Fired events go through normal ring buffer + subscriber dispatch.

        Returns: number of events fired (int).
        """
        now = time.time()
        ready: list[dict] = []

        with self._lock:
            remaining: list[dict] = []
            for entry in self._deferred:
                valid_until = entry.get("valid_until")
                valid_from = entry["valid_from"]

                # Expired before firing — discard silently
                if valid_until is not None and valid_until < now:
                    if self._debug:
                        log.info(
                            f"TICK_EXPIRED event_id={entry['event_id']} "
                            f"event_type={entry['event']['event']}"
                        )
                    continue

                if valid_from <= now:
                    # Ready to fire — collect in order (list is sorted by valid_from)
                    ready.append(entry)
                else:
                    # List is sorted ascending; all subsequent entries are also future
                    remaining.append(entry)

            self._deferred = remaining

        # Fire outside lock (avoids re-entrant lock if subscriber calls emit)
        for entry in ready:
            self._write_to_ring(entry["event"])
            self._wake_subscribers(entry["event"]["event"])
            if self._debug:
                log.info(
                    f"TICK_FIRED event_id={entry['event_id']} "
                    f"event_type={entry['event']['event']}"
                )

        return len(ready)

    def cancel(self, event_id: str) -> bool:
        """Cancel a deferred event before it fires.

        Returns True if the event was found and removed from the deferred queue.
        Returns False if already fired, expired, or not found.
        """
        with self._lock:
            for i, entry in enumerate(self._deferred):
                if entry["event_id"] == event_id:
                    del self._deferred[i]
                    if self._debug:
                        log.info(f"CANCEL_OK event_id={event_id}")
                    return True
        if self._debug:
            log.info(f"CANCEL_MISS event_id={event_id}")
        return False


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
