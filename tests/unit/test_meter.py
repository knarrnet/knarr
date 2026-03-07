from unittest.mock import patch

from knarr.commerce.admission_pipeline import AdmissionContext, run_admission
from knarr.dht.storage import Storage


def test_meter_increment_get_and_reset():
    storage = Storage(":memory:")
    state1 = storage.meter_increment("actor-a", "skill-a", "", 0)
    state2 = storage.meter_increment("actor-a", "skill-a", "", 0)
    fetched = storage.meter_get("actor-a", "skill-a", "")
    assert state1["count"] == 1
    assert state2["count"] == 2
    assert fetched["count"] == 2
    storage.meter_reset("actor-a", "skill-a", "")
    assert storage.meter_get("actor-a", "skill-a", "") is None
    storage.close()


def test_meter_window_expiry_resets_count():
    storage = Storage(":memory:")
    with patch("knarr.dht.storage.time.time", return_value=1000.0):
        storage.meter_increment("actor-a", "skill-a", "", 10)
    with patch("knarr.dht.storage.time.time", return_value=1015.0):
        assert storage.meter_get("actor-a", "skill-a", "") is None
        state = storage.meter_increment("actor-a", "skill-a", "", 10)
    assert state["count"] == 1
    storage.close()


def test_meter_is_independent_per_actor_skill_and_qualifier():
    storage = Storage(":memory:")
    storage.meter_increment("actor-a", "skill-a", "q1", 0)
    storage.meter_increment("actor-a", "skill-a", "q2", 0)
    storage.meter_increment("actor-b", "skill-a", "q1", 0)
    assert storage.meter_get("actor-a", "skill-a", "q1")["count"] == 1
    assert storage.meter_get("actor-a", "skill-a", "q2")["count"] == 1
    assert storage.meter_get("actor-b", "skill-a", "q1")["count"] == 1
    storage.close()


def test_admission_pipeline_rate_limits_when_meter_reaches_cap():
    result = run_admission(
        AdmissionContext(
            caller_key="a" * 64,
            skill_name="demo",
            base_price=1.0,
            balance=10.0,
            meter_count=3,
            meter_max_count=3,
        )
    )
    assert result.gate.outcome == "hard_block"
    assert "Rate limit exceeded" in (result.gate.reason or "")
