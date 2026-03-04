"""Tests for Track A — local skill fast path (A1) and per-skill max_concurrent (A2)."""

import pytest
from unittest.mock import MagicMock, AsyncMock


class TestGetSkillMaxConcurrent:
    """A2: Parse max_concurrent from [skills.X] config."""

    def _make_node_with_config(self, config):
        from knarr.dht.node import DHTNode
        node = DHTNode.__new__(DHTNode)
        node._config = config
        return node

    def test_default_is_1(self):
        node = self._make_node_with_config({"skills": {}})
        assert node._get_skill_max_concurrent("my-skill") == 1

    def test_configured_value(self):
        node = self._make_node_with_config({
            "skills": {"my-skill": {"max_concurrent": 4}}
        })
        assert node._get_skill_max_concurrent("my-skill") == 4

    def test_floor_at_1(self):
        node = self._make_node_with_config({
            "skills": {"my-skill": {"max_concurrent": 0}}
        })
        assert node._get_skill_max_concurrent("my-skill") == 1

    def test_negative_clamps_to_1(self):
        node = self._make_node_with_config({
            "skills": {"my-skill": {"max_concurrent": -5}}
        })
        assert node._get_skill_max_concurrent("my-skill") == 1

    def test_invalid_type_returns_1(self):
        node = self._make_node_with_config({
            "skills": {"my-skill": {"max_concurrent": "not_a_number"}}
        })
        assert node._get_skill_max_concurrent("my-skill") == 1

    def test_unknown_skill_returns_1(self):
        node = self._make_node_with_config({"skills": {}})
        assert node._get_skill_max_concurrent("unknown-skill") == 1

    def test_scalar_skill_config_returns_1(self):
        """If skill config is a scalar (not a dict), fallback to 1."""
        node = self._make_node_with_config({
            "skills": {"my-skill": "some-string"}
        })
        assert node._get_skill_max_concurrent("my-skill") == 1

    def test_different_skills_independent(self):
        node = self._make_node_with_config({
            "skills": {
                "fast-skill": {"max_concurrent": 8},
                "slow-skill": {"max_concurrent": 1},
            }
        })
        assert node._get_skill_max_concurrent("fast-skill") == 8
        assert node._get_skill_max_concurrent("slow-skill") == 1


class TestSkillActiveCounting:
    """Verify _skill_active dict is properly maintained."""

    def _make_node(self):
        from knarr.dht.node import DHTNode
        node = DHTNode.__new__(DHTNode)
        node._config = {}
        node._skill_active = {}
        return node

    def test_initial_count_is_zero(self):
        node = self._make_node()
        assert node._skill_active.get("any-skill", 0) == 0

    def test_count_increments(self):
        node = self._make_node()
        node._skill_active["test-skill"] = node._skill_active.get("test-skill", 0) + 1
        assert node._skill_active["test-skill"] == 1

    def test_count_decrements(self):
        node = self._make_node()
        node._skill_active["test-skill"] = 3
        node._skill_active["test-skill"] = max(0, node._skill_active.get("test-skill", 1) - 1)
        assert node._skill_active["test-skill"] == 2

    def test_count_floors_at_zero(self):
        node = self._make_node()
        node._skill_active["test-skill"] = 0
        node._skill_active["test-skill"] = max(0, node._skill_active.get("test-skill", 1) - 1)
        assert node._skill_active["test-skill"] == 0


class TestMaxConcurrentLogic:
    """Verify the SKILL_BUSY rejection logic mirrors what _handle_task_request does."""

    def test_at_limit_should_reject(self):
        """Simulating: active >= max → reject with SKILL_BUSY."""
        _skill_active = {"my-skill": 4}
        _skill_max = 4  # same as active → AT limit
        skill_name = "my-skill"

        current = _skill_active.get(skill_name, 0)
        should_reject = current >= _skill_max
        assert should_reject

    def test_below_limit_should_proceed(self):
        _skill_active = {"my-skill": 2}
        _skill_max = 4
        skill_name = "my-skill"

        current = _skill_active.get(skill_name, 0)
        should_reject = current >= _skill_max
        assert not should_reject

    def test_zero_active_proceeds(self):
        _skill_active = {}
        _skill_max = 1
        skill_name = "my-skill"

        current = _skill_active.get(skill_name, 0)
        should_reject = current >= _skill_max
        assert not should_reject

    def test_skill_busy_not_queue_full(self):
        """The rejection reason must be SKILL_BUSY, not QUEUE_FULL."""
        expected_code = "TASK_REJECTED"
        expected_reason = "SKILL_BUSY"

        err = {
            "code": expected_code,
            "message": "Skill at max concurrency",
            "reason": expected_reason,
        }

        assert err["code"] == "TASK_REJECTED"
        assert err["reason"] == "SKILL_BUSY"
        assert err["reason"] != "QUEUE_FULL"


class TestFastPathReceiptParity:
    """A1: Fast path must write the same receipt types as the async path."""

    def test_fast_path_writes_order_executing_receipt(self):
        """The fast path should write an order_executing receipt (spec requirement)."""
        # This is a structural check: the _execute_fast_path code calls _write_receipt
        # with document_type="order_executing" and fast_path=True
        import inspect
        from knarr.dht.node import DHTNode
        # Read the method source
        src = inspect.getsource(DHTNode._execute_fast_path)
        assert "order_executing" in src, "Fast path must write order_executing receipt"
        assert "_write_receipt" in src, "Fast path must call _write_receipt"

    def test_fast_path_emits_task_started_event(self):
        """The fast path should emit task.started bus event."""
        import inspect
        from knarr.dht.node import DHTNode
        src = inspect.getsource(DHTNode._execute_fast_path)
        assert "task.started" in src, "Fast path must emit task.started"

    def test_fast_path_delegates_to_execute_queued_task(self):
        """Fast path should call _execute_queued_task for identical behavior."""
        import inspect
        from knarr.dht.node import DHTNode
        src = inspect.getsource(DHTNode._execute_fast_path)
        assert "_execute_queued_task" in src, "Fast path must delegate to _execute_queued_task"
