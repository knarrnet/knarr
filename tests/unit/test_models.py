from knarr.core.models import SkillSheet

def test_skill_sheet_roundtrip():
    data = {
        "name": "test",
        "version": "1.1.1",
        "description": "desc",
        "tags": ["a", "b"],
        "input_schema": {"in": "str"},
        "output_schema": {"out": "str"}
    }
    sheet = SkillSheet.from_dict(data)
    assert sheet.to_dict() == data