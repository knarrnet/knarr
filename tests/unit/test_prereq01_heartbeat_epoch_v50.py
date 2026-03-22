"""PREREQ-01: punchhole_epoch field in Heartbeat.

BUG: Heartbeat dataclass lacks punchhole_epoch field. Indexers have no
efficient way to know when to re-crawl punchhole caches.

FIX: Add punchhole_epoch: int = 0 to Heartbeat dataclass. Increment in
punchhole backend _stale_loop when any cache object is staled.
"""
import pytest


def test_heartbeat_has_punchhole_epoch_field():
    """Heartbeat dataclass must have punchhole_epoch field."""
    from knarr.core.messages import Heartbeat

    hb = Heartbeat(node_id="aa" * 32, timestamp=1710000000.0, version="0.50.0")
    assert hasattr(hb, "punchhole_epoch"), (
        "PREREQ-01: Heartbeat missing punchhole_epoch field"
    )
    assert hb.punchhole_epoch == 0, (
        f"PREREQ-01: punchhole_epoch default should be 0, got {hb.punchhole_epoch}"
    )


def test_heartbeat_punchhole_epoch_serializes():
    """Heartbeat serializes/deserializes with punchhole_epoch intact."""
    from knarr.core.messages import Heartbeat
    import dataclasses

    hb = Heartbeat(node_id="bb" * 32, timestamp=1710000001.0, version="0.50.0", punchhole_epoch=7)
    hb_dict = dataclasses.asdict(hb)

    assert "punchhole_epoch" in hb_dict, (
        "PREREQ-01: punchhole_epoch not in serialized dict"
    )
    assert hb_dict["punchhole_epoch"] == 7


def test_heartbeat_punchhole_epoch_backward_compatible():
    """Old nodes ignore unknown heartbeat fields (Heartbeat default = 0)."""
    from knarr.core.messages import Heartbeat

    # Construct without providing punchhole_epoch (default value)
    hb = Heartbeat(node_id="cc" * 32, timestamp=1710000002.0)
    assert hb.punchhole_epoch == 0, (
        "PREREQ-01: punchhole_epoch must default to 0 for backward compatibility"
    )


def test_punchhole_epoch_field_type_is_int():
    """punchhole_epoch must be int type (not str or float)."""
    from knarr.core.messages import Heartbeat
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(Heartbeat)}
    assert "punchhole_epoch" in fields, "PREREQ-01: punchhole_epoch field not found"
    field = fields["punchhole_epoch"]
    assert field.default == 0, f"PREREQ-01: expected default=0, got {field.default}"
