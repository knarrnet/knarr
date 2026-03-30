"""E-01 tests: Envelope dataclass + deserialize registration."""
import json


def test_envelope_serialize_roundtrip():
    """Envelope survives serialize → deserialize."""
    from knarr.core.messages import Envelope, serialize_message, deserialize_message
    env = Envelope(
        uri="knarr://deadbeef/s/llm/chat@1.0",
        payload='{"kind":"test"}',
        timestamp="2026-03-30T12:34:56Z",
        trace_id="trace-123",
    )
    raw = serialize_message(env)
    restored = deserialize_message(raw)
    assert isinstance(restored, Envelope)
    assert restored.uri == env.uri
    assert restored.payload == env.payload
    assert restored.timestamp == env.timestamp
    assert restored.trace_id == env.trace_id
    assert restored.type == "ENVELOPE"


def test_envelope_empty_uri_is_legacy():
    """Empty URI preserves the legacy-routing fallback."""
    from knarr.core.messages import Envelope
    env = Envelope()
    assert env.uri == ""
    assert env.payload == ""
    assert env.trace_id == ""


def test_envelope_in_type_map():
    """deserialize_message knows how to reconstruct Envelope."""
    from knarr.core.messages import deserialize_message
    raw = json.dumps({
        "type": "ENVELOPE",
        "uri": "knarr:///p/catalog",
        "payload": "{}",
        "trace_id": "tr-1",
    }).encode()
    msg = deserialize_message(raw)
    assert msg.type == "ENVELOPE"
    assert msg.uri == "knarr:///p/catalog"
    assert msg.trace_id == "tr-1"


def test_envelope_trace_id_propagates_through_roundtrip():
    """trace_id is preserved when deserializing from raw JSON."""
    from knarr.core.messages import deserialize_message
    raw = json.dumps({
        "type": "ENVELOPE",
        "uri": "",
        "payload": '{"ok":true}',
        "trace_id": "trace-propagates",
    }).encode()
    restored = deserialize_message(raw)
    assert restored.trace_id == "trace-propagates"
