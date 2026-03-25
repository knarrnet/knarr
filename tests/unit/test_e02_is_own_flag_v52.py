"""E-02: upsert_skill() preserves is_own=1 on DHT re-announcement.

When a DHT re-announcement arrives for an already-registered own skill,
upsert_skill() must NOT overwrite is_own=1 with is_own=0.

Fix uses: ON CONFLICT DO UPDATE SET is_own = MAX(is_own, excluded.is_own)
"""

import pytest
from knarr.dht.storage import Storage
from knarr.core.models import SkillSheet


def make_skill_sheet(name="test-skill"):
    return SkillSheet(
        name=name,
        version="1.0.0",
        description="Test skill",
        tags=[],
        input_schema={},
        output_schema={},
    )


# ──────────────────────────────────────────────────────────────────────────────
# E-02-A: is_own preserved when re-announced as non-own
# ──────────────────────────────────────────────────────────────────────────────

def test_is_own_preserved_on_dht_reannouncement():
    """is_own=1 must survive a DHT re-announcement with is_own=False."""
    storage = Storage(":memory:")
    node_id = "a" * 64
    skill_key = "test-skill"
    skill_sheet = make_skill_sheet()

    # Register as own skill
    storage.upsert_skill(skill_key, node_id, skill_sheet, ttl=600, is_own=True)

    # Verify is_own=1
    own_skills = storage.get_own_skills()
    assert len(own_skills) == 1

    # DHT re-announcement arrives with is_own=False
    storage.upsert_skill(skill_key, node_id, skill_sheet, ttl=600, is_own=False)

    # is_own must still be 1
    own_skills_after = storage.get_own_skills()
    assert len(own_skills_after) == 1, (
        "is_own=1 was overwritten to 0 by DHT re-announcement — this is E-02 bug"
    )


# ──────────────────────────────────────────────────────────────────────────────
# E-02-B: is_own can be promoted from 0 to 1
# ──────────────────────────────────────────────────────────────────────────────

def test_is_own_promoted_from_zero_to_one():
    """is_own=0 can be upgraded to is_own=1 by a second upsert."""
    storage = Storage(":memory:")
    node_id = "b" * 64
    skill_key = "other-skill"
    skill_sheet = make_skill_sheet("other-skill")

    # First: register as non-own (gossip-received)
    storage.upsert_skill(skill_key, node_id, skill_sheet, ttl=600, is_own=False)
    own_skills = storage.get_own_skills()
    assert len(own_skills) == 0

    # Then: mark as own (local registration)
    storage.upsert_skill(skill_key, node_id, skill_sheet, ttl=600, is_own=True)
    own_skills_after = storage.get_own_skills()
    assert len(own_skills_after) == 1


# ──────────────────────────────────────────────────────────────────────────────
# E-02-C: multiple skills, only own ones preserved
# ──────────────────────────────────────────────────────────────────────────────

def test_multiple_skills_own_flag_isolation():
    """Only specifically-marked own skills appear in get_own_skills()."""
    storage = Storage(":memory:")
    node_id = "c" * 64

    storage.upsert_skill("skill-a", node_id, make_skill_sheet("skill-a"), ttl=600, is_own=True)
    storage.upsert_skill("skill-b", node_id, make_skill_sheet("skill-b"), ttl=600, is_own=False)
    storage.upsert_skill("skill-c", node_id, make_skill_sheet("skill-c"), ttl=600, is_own=True)

    own_skills = storage.get_own_skills()
    own_names = {s.name for s in own_skills}
    assert "skill-a" in own_names
    assert "skill-c" in own_names
    assert "skill-b" not in own_names

    # Now re-announce skill-a and skill-c as non-own (DHT gossip)
    storage.upsert_skill("skill-a", node_id, make_skill_sheet("skill-a"), ttl=600, is_own=False)
    storage.upsert_skill("skill-c", node_id, make_skill_sheet("skill-c"), ttl=600, is_own=False)

    own_skills_after = storage.get_own_skills()
    own_names_after = {s.name for s in own_skills_after}
    assert "skill-a" in own_names_after, "skill-a is_own was overwritten by gossip"
    assert "skill-c" in own_names_after, "skill-c is_own was overwritten by gossip"


# ──────────────────────────────────────────────────────────────────────────────
# E-02-D: is_own preserved across multiple re-announcements
# ──────────────────────────────────────────────────────────────────────────────

def test_is_own_preserved_across_many_reannouncements():
    """is_own=1 survives many sequential DHT re-announcements."""
    storage = Storage(":memory:")
    node_id = "d" * 64
    skill_key = "stable-skill"

    storage.upsert_skill(skill_key, node_id, make_skill_sheet(skill_key), ttl=600, is_own=True)

    for _ in range(10):
        storage.upsert_skill(skill_key, node_id, make_skill_sheet(skill_key), ttl=600, is_own=False)

    own_skills = storage.get_own_skills()
    assert len(own_skills) == 1, "is_own=1 lost after repeated non-own re-announcements"
