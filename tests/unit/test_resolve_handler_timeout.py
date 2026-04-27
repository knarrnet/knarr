"""A-10: unit tests for DHTNode._resolve_handler_timeout.

Precedence: msg.timeout_ms → skill_cfg["timeout"] → skills.default_timeout,
capped at node.max_task_timeout.
"""
from unittest.mock import MagicMock

import pytest

from knarr.dht.node import DHTNode


def _make_node(cfg):
    node = DHTNode.__new__(DHTNode)
    node._config = cfg
    return node


def _msg(timeout_ms=0):
    m = MagicMock()
    m.timeout_ms = timeout_ms
    return m


def test_msg_timeout_wins_over_skill_and_default():
    node = _make_node({"skills": {"default_timeout": 30}, "node": {"max_task_timeout": 3600}})
    assert node._resolve_handler_timeout(_msg(timeout_ms=7000), {"timeout": 90}) == 7.0


def test_skill_cfg_timeout_used_when_msg_unset():
    node = _make_node({"skills": {"default_timeout": 30}, "node": {"max_task_timeout": 3600}})
    assert node._resolve_handler_timeout(_msg(timeout_ms=0), {"timeout": 90}) == 90.0


def test_default_timeout_used_when_msg_and_skill_unset():
    node = _make_node({"skills": {"default_timeout": 45}, "node": {"max_task_timeout": 3600}})
    assert node._resolve_handler_timeout(_msg(timeout_ms=0), {}) == 45.0


def test_default_timeout_used_when_skill_cfg_is_none():
    node = _make_node({"skills": {"default_timeout": 45}, "node": {"max_task_timeout": 3600}})
    assert node._resolve_handler_timeout(_msg(timeout_ms=0), None) == 45.0


def test_max_cap_applied_to_msg_timeout():
    node = _make_node({"skills": {"default_timeout": 30}, "node": {"max_task_timeout": 60}})
    # msg requested 120s, cap is 60s → 60s
    assert node._resolve_handler_timeout(_msg(timeout_ms=120_000), {"timeout": 5}) == 60.0


def test_max_cap_zero_disables_capping():
    node = _make_node({"skills": {"default_timeout": 30}, "node": {"max_task_timeout": 0}})
    assert node._resolve_handler_timeout(_msg(timeout_ms=999_000), {}) == 999.0


def test_invalid_skill_timeout_falls_through_to_default():
    node = _make_node({"skills": {"default_timeout": 25}, "node": {"max_task_timeout": 3600}})
    assert node._resolve_handler_timeout(_msg(timeout_ms=0), {"timeout": "not-a-number"}) == 25.0


def test_nonpositive_skill_timeout_falls_through_to_default():
    node = _make_node({"skills": {"default_timeout": 25}, "node": {"max_task_timeout": 3600}})
    assert node._resolve_handler_timeout(_msg(timeout_ms=0), {"timeout": 0}) == 25.0
