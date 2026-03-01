from knarr.core.messages import (
    JoinRequest, JoinResponse, Announce, Query, QueryResponse,
    Deregister, Heartbeat, Ack, TaskRequest, TaskStatus, TaskResult,
    SyncRequest, SyncResponse, Warn, Blocked, MailSync, MailAck, serialize_message, deserialize_message, sign_message
)
from nacl.signing import SigningKey

def test_message_roundtrip():
    key = SigningKey.generate()
    messages = [
        JoinRequest(node_id="n1", host="h", port=1),
        JoinResponse(peers=[{"node_id": "n1", "host": "h", "port": 1}]),
        Announce(node_id="n1", skill_key="k", skill_sheet={"name": "k", "version": "1.0.0", "description": "d", "tags": ["t1"], "input_schema": {}, "output_schema": {}}),
        Query(query_type="tag", value="v"),
        QueryResponse(results=[{"node_id": "n1", "host": "h", "port": 1, "skill_sheet": {"name":"k", "tags":["t1"]}}]),
        Deregister(node_id="n1", skill_key="k"),
        Heartbeat(node_id="n1", timestamp=1.0),
        Ack(status="ok", error_detail="none"),
        TaskRequest(task_id="t1", requester_node_id="n1", requester_host="h", requester_port=1, skill_name="s", input_data={"a": 1}),
        TaskStatus(task_id="t1", status="accepted", reason="r"),
        TaskResult(task_id="t1", status="completed", output_data={"b": 2}, error={"c": 3}),
        SyncRequest(since=100.0),
        SyncResponse(skills=[{"skill_key": "k", "provider_node_id": "n1", "skill_sheet": {}}]),
        Warn(delay_ms=100, reason="rate_limit", pressure_pct=50),
        Blocked(reason="rate_limit_exceeded", duration_minutes=30),
        MailSync(sender_node_id="n1", items=[{"item_id": "i1", "body": {"text": "hello"}}], batch_seq=5),
        MailAck(sender_node_id="n1", ack_seq=5, item_ids=["i1"]),
    ]
    for msg in messages:
        # Sign before serializing
        signed = sign_message(msg, key)
        data = serialize_message(signed)
        decoded = deserialize_message(data)
        assert type(decoded) is type(msg)
        assert decoded.type == msg.type
        assert decoded.public_key == key.verify_key.encode().hex()
        assert decoded.signature != ""
        if hasattr(msg, 'task_id'):
            assert decoded.task_id == msg.task_id
