import pytest
import uuid
from knarr.core.messages import (
    SyncRequest, SyncResponse, Announce, Deregister
)

def test_sync_response_serialization():
    skills = [
        {"skill_key": "k1", "provider_node_id": "n1", "skill_sheet": {"name": "k1", "tags": ["t1"]}}
    ]
    msg = SyncResponse(skills=skills)
    d = msg.to_dict()
    assert d["skills"] == skills

def test_sync_request_serialization():
    msg = SyncRequest(since=123.45)
    d = msg.to_dict()
    assert d["since"] == 123.45

def test_announce_with_hops():
    msg = Announce(hops=5)
    assert msg.hops == 5

def test_deregister_with_hops():
    msg = Deregister(hops=3)
    assert msg.hops == 3
