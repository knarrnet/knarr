"""Sentinel tests for v0.25.x — method injection safety."""
import json
import types


def test_execute_queued_task_strips_callables_from_result():
    """Result data from handlers must be stripped of callable values before serialization.

    Bug: handlers may echo input_data which has _node_encrypt/_node_decrypt methods injected.
    Without stripping, json.dumps(result_data) raises TypeError.
    Sentinel: ensures the strip pattern exists in _execute_queued_task.
    """
    import inspect
    from knarr.dht.node import DHTNode
    source = inspect.getsource(DHTNode._execute_queued_task)
    # The callable-stripping pattern must exist
    assert "not callable(v)" in source, (
        "_execute_queued_task must strip callable values from result_data "
        "to prevent 'Object of type method is not JSON serializable' errors"
    )


def test_callable_in_dict_not_json_serializable():
    """Verify that a dict with a method value cannot be json.dumps'd.

    This is the root cause — if this ever becomes serializable, the sentinel
    above would be unnecessary (but shouldn't be removed without review).
    """
    d = {"key": "value", "method": lambda: None}
    try:
        json.dumps(d)
        assert False, "json.dumps should fail on callable values"
    except TypeError:
        pass  # Expected


def test_strip_callable_pattern():
    """The strip pattern must remove callables but keep everything else."""
    def fake_method():
        pass

    data = {
        "result": "ok",
        "count": 42,
        "_node_encrypt": fake_method,
        "_node_decrypt": fake_method,
        "_caller_node_id": "abc123",
    }
    stripped = {k: v for k, v in data.items() if not callable(v)}
    assert "result" in stripped
    assert "count" in stripped
    assert "_caller_node_id" in stripped
    assert "_node_encrypt" not in stripped
    assert "_node_decrypt" not in stripped
    # Must be JSON serializable after stripping
    json.dumps(stripped)
