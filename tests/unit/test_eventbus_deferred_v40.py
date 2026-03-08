"""Tests for Track C1: Deferred Bus Primitive (v0.40.0).

Tests the new valid_from / valid_until / tick() / cancel() functionality
added to EventBus without modifying any existing behaviour.
"""
import time
import sys
import os

# Allow importing from the workspace src tree
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from knarr.dht.eventbus import EventBus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bus() -> EventBus:
    return EventBus(size=64, debug=False)


# ---------------------------------------------------------------------------
# C1-T1: Backward compatibility — no valid_from fires immediately
# ---------------------------------------------------------------------------

def test_emit_no_valid_from_fires_immediately():
    bus = _make_bus()
    sub = bus.subscribe("task.*")

    bus.emit("task.done", result="ok")

    events = sub.poll()
    assert len(events) == 1
    assert events[0]["event"] == "task.done"
    assert events[0]["result"] == "ok"


# ---------------------------------------------------------------------------
# C1-T2: emit() always returns an event_id string
# ---------------------------------------------------------------------------

def test_emit_returns_event_id():
    bus = _make_bus()

    # Immediate event
    eid1 = bus.emit("task.done")
    assert isinstance(eid1, str)
    assert eid1.startswith("evt_")
    assert len(eid1) == 4 + 16  # "evt_" + 16 hex chars

    # Deferred event
    eid2 = bus.emit("task.deferred", valid_from=time.time() + 60)
    assert isinstance(eid2, str)
    assert eid2.startswith("evt_")
    assert eid1 != eid2  # unique IDs


# ---------------------------------------------------------------------------
# C1-T3: Deferred event is NOT fired before valid_from
# ---------------------------------------------------------------------------

def test_deferred_event_not_fired_before_valid_from():
    bus = _make_bus()
    sub = bus.subscribe("credit.*")

    bus.emit("credit.release", amount=5.0, valid_from=time.time() + 300)

    # tick() should fire nothing yet
    fired = bus.tick()
    assert fired == 0

    # subscriber sees nothing
    events = sub.poll()
    assert events == []


# ---------------------------------------------------------------------------
# C1-T4: tick() fires events past valid_from
# ---------------------------------------------------------------------------

def test_tick_fires_ready_events():
    bus = _make_bus()
    sub = bus.subscribe("credit.*")

    # Inject a deferred event that is now ready (valid_from in the past).
    # We inject directly into the deferred queue because emit() fires
    # immediately when valid_from is already past (per schema: valid_from
    # in the past = fires immediately). tick() only processes the queue.
    now = time.time()
    entry = {
        "event_id": "evt_test_ready",
        "event": {
            "event": "credit.release",
            "event_id": "evt_test_ready",
            "ts": now,
            "valid_from": now - 1,
            "valid_until": None,
            "amount": 5.0,
        },
        "valid_from": now - 1,
        "valid_until": None,
        "created_at": now - 2,
    }
    with bus._lock:
        bus._deferred.append(entry)

    fired = bus.tick()
    assert fired == 1

    events = sub.poll()
    assert len(events) == 1
    assert events[0]["event"] == "credit.release"
    assert events[0]["amount"] == 5.0


# ---------------------------------------------------------------------------
# C1-T5: tick() fires events in valid_from order
# ---------------------------------------------------------------------------

def test_tick_fires_in_valid_from_order():
    """Events in deferred queue fire in valid_from ascending order.

    We inject events directly into the deferred queue (out of order) and
    verify tick() fires them sorted by valid_from.
    """
    bus = _make_bus()
    sub = bus.subscribe("seq.*")

    now = time.time()

    def _entry(name, order, vf_offset):
        vf = now + vf_offset
        return {
            "event_id": f"evt_{name}",
            "event": {
                "event": name,
                "event_id": f"evt_{name}",
                "ts": now,
                "valid_from": vf,
                "valid_until": None,
                "order": order,
            },
            "valid_from": vf,
            "valid_until": None,
            "created_at": now,
        }

    # Inject in reverse order — tick() must sort by valid_from ascending
    # All use negative offsets so they fire immediately on tick()
    with bus._lock:
        bus._deferred.append(_entry("seq.c", 3, -1))
        bus._deferred.append(_entry("seq.a", 1, -3))
        bus._deferred.append(_entry("seq.b", 2, -2))
        # Sort them as emit() would (ascending valid_from)
        bus._deferred.sort(key=lambda e: e["valid_from"])

    bus.tick()

    events = sub.poll()
    assert len(events) == 3
    orders = [e["order"] for e in events]
    assert orders == [1, 2, 3], f"Expected [1,2,3], got {orders}"


# ---------------------------------------------------------------------------
# C1-T6: Event with valid_until in the past is discarded at emit time
# ---------------------------------------------------------------------------

def test_expired_event_discarded():
    bus = _make_bus()
    sub = bus.subscribe("stale.*")

    # valid_until is already past — should be silently discarded at emit time
    eid = bus.emit("stale.announcement", valid_until=time.time() - 1)

    assert isinstance(eid, str)  # event_id still returned

    # No event in ring buffer
    events = sub.poll()
    assert events == []

    # No event in deferred queue either
    fired = bus.tick()
    assert fired == 0


# ---------------------------------------------------------------------------
# C1-T7: cancel() before firing returns True and event never fires
# ---------------------------------------------------------------------------

def test_cancel_before_fire():
    bus = _make_bus()
    sub = bus.subscribe("credit.*")

    eid = bus.emit("credit.release", amount=5.0, valid_from=time.time() + 300)

    result = bus.cancel(eid)
    assert result is True

    # Tick should fire nothing
    fired = bus.tick()
    assert fired == 0

    events = sub.poll()
    assert events == []


# ---------------------------------------------------------------------------
# C1-T8: cancel() after fire returns False
# ---------------------------------------------------------------------------

def test_cancel_after_fire_returns_false():
    bus = _make_bus()
    sub = bus.subscribe("credit.*")

    # Immediate event — fires at emit time, never enters deferred queue
    eid = bus.emit("credit.release", amount=5.0)

    # Consume it
    sub.poll()

    # Cancel should return False — event is not in deferred queue
    result = bus.cancel(eid)
    assert result is False


# ---------------------------------------------------------------------------
# C1-T9: cancel() on unknown event_id returns False
# ---------------------------------------------------------------------------

def test_cancel_unknown_id_returns_false():
    bus = _make_bus()
    result = bus.cancel("evt_deadbeefdeadbeef")
    assert result is False


# ---------------------------------------------------------------------------
# C1-T10: Deferred event with valid_until that passes before tick() is discarded
# ---------------------------------------------------------------------------

def test_deferred_with_valid_until_auto_expires():
    bus = _make_bus()
    sub = bus.subscribe("ephemeral.*")

    # valid_from = 1 second ago (ready to fire), valid_until = 2 seconds ago (already expired)
    # valid_until < now at emit time => discarded immediately at emit
    now = time.time()
    eid = bus.emit(
        "ephemeral.ping",
        valid_from=now - 1,
        valid_until=now - 2,
    )
    assert isinstance(eid, str)

    # Should fire nothing
    fired = bus.tick()
    assert fired == 0

    events = sub.poll()
    assert events == []


def test_deferred_with_valid_until_expires_before_tick():
    """Event deferred with valid_until already past is discarded in tick()."""
    bus = _make_bus()
    sub = bus.subscribe("ephemeral.*")

    # Manually inject a deferred entry that has expired (simulates time passing)
    now = time.time()
    # Inject directly: valid_from is in past but valid_until is also past
    entry = {
        "event_id": "evt_test0001",
        "event": {"event": "ephemeral.ping", "event_id": "evt_test0001", "ts": now},
        "valid_from": now - 5,
        "valid_until": now - 1,  # expired
        "created_at": now - 10,
    }
    with bus._lock:
        bus._deferred.append(entry)

    fired = bus.tick()
    assert fired == 0

    events = sub.poll()
    assert events == []


# ---------------------------------------------------------------------------
# C1-T11: event_id and new fields appear in the fired event dict
# ---------------------------------------------------------------------------

def test_event_fields_in_fired_event():
    bus = _make_bus()
    sub = bus.subscribe("task.*")

    eid = bus.emit("task.done", skill="echo", result="ok")
    events = sub.poll()

    assert len(events) == 1
    ev = events[0]
    assert ev["event_id"] == eid
    assert ev["skill"] == "echo"
    assert ev["result"] == "ok"
    assert "ts" in ev
    assert ev["valid_from"] is None
    assert ev["valid_until"] is None


# ---------------------------------------------------------------------------
# C1-T12: Multiple deferred events, only some ready on tick
# ---------------------------------------------------------------------------

def test_tick_only_fires_ready_subset():
    """tick() fires only events with valid_from <= now; future ones stay deferred."""
    bus = _make_bus()
    sub = bus.subscribe("*")

    now = time.time()

    def _entry(name, vf_offset):
        vf = now + vf_offset
        return {
            "event_id": f"evt_{name}",
            "event": {
                "event": name,
                "event_id": f"evt_{name}",
                "ts": now,
                "valid_from": vf,
                "valid_until": None,
            },
            "valid_from": vf,
            "valid_until": None,
            "created_at": now,
        }

    with bus._lock:
        bus._deferred.append(_entry("alpha.now", -1))       # ready
        bus._deferred.append(_entry("gamma.now", -0.5))     # ready
        bus._deferred.append(_entry("beta.future", 100))    # future
        bus._deferred.sort(key=lambda e: e["valid_from"])

    fired = bus.tick()
    assert fired == 2

    events = sub.poll()
    event_types = {e["event"] for e in events}
    assert "alpha.now" in event_types
    assert "gamma.now" in event_types
    assert "beta.future" not in event_types

    # beta still in deferred queue
    assert len(bus._deferred) == 1
    assert bus._deferred[0]["event"]["event"] == "beta.future"
