import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from knarr.core.models import SkillSheet
from knarr.dht.storage import Storage


def _sheet(name: str = "skill-a") -> SkillSheet:
    return SkillSheet(
        name=name,
        version="1.0.0",
        description="test skill",
        tags=["test"],
        input_schema={},
        output_schema={},
    )


def _is_own_flag(storage: Storage, skill_key: str, provider_node_id: str) -> int:
    row = storage._get_conn().execute(
        "SELECT is_own FROM skills WHERE skill_key = ? AND provider_node_id = ?",
        (skill_key, provider_node_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_is_own_true_then_false_stays_true():
    storage = Storage(":memory:")
    storage.upsert_skill("skill-a", "node-1", _sheet(), is_own=True)
    storage.upsert_skill("skill-a", "node-1", _sheet(), is_own=False)

    assert _is_own_flag(storage, "skill-a", "node-1") == 1


def test_is_own_false_then_true_becomes_true():
    storage = Storage(":memory:")
    storage.upsert_skill("skill-a", "node-1", _sheet(), is_own=False)
    storage.upsert_skill("skill-a", "node-1", _sheet(), is_own=True)

    assert _is_own_flag(storage, "skill-a", "node-1") == 1


def test_is_own_false_then_false_stays_false():
    storage = Storage(":memory:")
    storage.upsert_skill("skill-a", "node-1", _sheet(), is_own=False)
    storage.upsert_skill("skill-a", "node-1", _sheet(), is_own=False)

    assert _is_own_flag(storage, "skill-a", "node-1") == 0


def test_get_own_skills_survives_false_reannouncement():
    storage = Storage(":memory:")
    storage.upsert_skill("skill-a", "node-1", _sheet("skill-a"), is_own=True)
    storage.upsert_skill("skill-a", "node-1", _sheet("skill-a"), is_own=False)

    own_skills = storage.get_own_skills()

    assert [skill.name for skill in own_skills] == ["skill-a"]
