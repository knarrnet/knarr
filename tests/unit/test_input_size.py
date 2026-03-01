import pytest
from knarr.core.models import SkillSheet
from knarr.core.validation import validate_skill_sheet, ValidationError

def test_max_input_size_default():
    sheet = SkillSheet(
        name="s", version="1.0.0", description="d", tags=["t"],
        input_schema={}, output_schema={}
    )
    assert sheet.max_input_size == 65536

def test_max_input_size_custom():
    data = {
        "name": "s", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}, "max_input_size": 131072
    }
    sheet = validate_skill_sheet(data)
    assert sheet.max_input_size == 131072

def test_max_input_size_validation():
    base = {
        "name": "s", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {}, "output_schema": {}
    }
    
    # 0 fails
    with pytest.raises(ValidationError, match="between 1 and 10485760"):
        validate_skill_sheet({**base, "max_input_size": 0})
        
    # -1 fails
    with pytest.raises(ValidationError, match="between 1 and 10485760"):
        validate_skill_sheet({**base, "max_input_size": -1})
        
    # > 10MB fails
    with pytest.raises(ValidationError, match="between 1 and 10485760"):
        validate_skill_sheet({**base, "max_input_size": 10485761})

def test_max_input_size_serialization():
    sheet = SkillSheet(
        name="s", version="1.0.0", description="d", tags=["t"],
        input_schema={}, output_schema={}, max_input_size=1024
    )
    d = sheet.to_dict()
    assert d["max_input_size"] == 1024
    
    sheet2 = SkillSheet.from_dict(d)
    assert sheet2.max_input_size == 1024

@pytest.mark.asyncio
async def test_max_input_size_rejects_oversized():
    from knarr.dht.node import DHTNode
    from knarr.core.messages import TaskRequest
    
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    
    # Register skill with small limit
    node.register_handler("small", lambda d: d)
    await node.announce({
        "name": "small", "version": "1.0.0", "description": "d", "tags": ["t"],
        "input_schema": {"data": "str"}, "output_schema": {}, "max_input_size": 100
    })
    
    try:
        # 1. Within limit (json of {"data": "ok"} is ~15 bytes)
        req1 = node._sign(TaskRequest(
            task_id="t1", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999,
            skill_name="small", input_data={"data": "ok"}
        ))
        resp1 = await node._process_message(req1)
        assert resp1.status == "completed"
        
        # 2. Over limit
        big_data = "x" * 200
        req2 = node._sign(TaskRequest(
            task_id="t2", requester_node_id="r", requester_host="127.0.0.1", requester_port=9999,
            skill_name="small", input_data={"data": big_data}
        ))
        resp2 = await node._process_message(req2)
        assert resp2.status == "failed"
        assert resp2.error["code"] == "INPUT_TOO_LARGE"
        
    finally:
        await node.stop()
