import asyncio
import pytest
from knarr.core.messages import Heartbeat, Query, QueryResponse
from knarr.dht.node import DHTNode
from knarr.core.models import SkillSheet
from unittest.mock import MagicMock, AsyncMock, patch

def test_heartbeat_no_load_field():
    """Heartbeat dataclass has no load or peer_count fields."""
    hb = Heartbeat(node_id="abc", timestamp=1.0, version="0.14.0")
    d = hb.to_dict()
    assert "load" not in d
    assert "peer_count" not in d
    assert "version" in d

@pytest.mark.asyncio
async def test_query_response_includes_load():  # SENTINEL
    """Load MUST remain in QueryResponse results. Do not remove."""
    node = DHTNode("127.0.0.1", 0)
    sheet = SkillSheet(
        name="test", version="1.0.0", description="test",
        tags=["test"], input_schema={}, output_schema={}
    )
    node._own_skills["test"] = sheet
    node._skill_visibility["test"] = "public"
    node._task_slots = 4
    node._active_workers = 1

    # Build a Query message and call _process_message directly
    query = Query(query_type="name", value="test")
    node._sign = lambda m: m  # bypass signing

    response = await node._process_message(query)
    assert isinstance(response, QueryResponse)
    assert len(response.results) >= 1

    # Find our own result
    own_result = [r for r in response.results if r["node_id"] == node.node_info.node_id]
    assert len(own_result) == 1
    assert "_load" in own_result[0], "Load must remain in QueryResponse results"
    assert own_result[0]["_load"] >= 0
