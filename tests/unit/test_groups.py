"""Tests for core GroupEngine interface."""
import pytest
from knarr.core.groups import DefaultGroupEngine


class TestDefaultGroupEngine:
    """Tests for the DefaultGroupEngine (TOML-backed fallback)."""

    def test_is_member_true(self):
        engine = DefaultGroupEngine({"partners": {"alice", "bob"}})
        assert engine.is_member("alice", "partners") is True

    def test_is_member_false(self):
        engine = DefaultGroupEngine({"partners": {"alice", "bob"}})
        assert engine.is_member("charlie", "partners") is False

    def test_is_member_unknown_group(self):
        engine = DefaultGroupEngine({"partners": {"alice"}})
        assert engine.is_member("alice", "nonexistent") is False

    def test_get_groups_single(self):
        engine = DefaultGroupEngine({"partners": {"alice"}, "vip": {"bob"}})
        assert engine.get_groups("alice") == ["partners"]

    def test_get_groups_multiple(self):
        engine = DefaultGroupEngine({
            "partners": {"alice"},
            "vip": {"alice"},
            "blocked": {"charlie"},
        })
        groups = engine.get_groups("alice")
        assert sorted(groups) == ["partners", "vip"]

    def test_get_groups_none(self):
        engine = DefaultGroupEngine({"partners": {"alice"}})
        assert engine.get_groups("stranger") == []

    def test_empty_engine(self):
        engine = DefaultGroupEngine({})
        assert engine.is_member("anyone", "anything") is False
        assert engine.get_groups("anyone") == []

    def test_multi_group_membership(self):
        engine = DefaultGroupEngine({
            "a": {"x", "y"},
            "b": {"y", "z"},
            "c": {"x", "z"},
        })
        assert engine.is_member("x", "a") is True
        assert engine.is_member("x", "b") is False
        assert engine.is_member("x", "c") is True
        assert sorted(engine.get_groups("y")) == ["a", "b"]

    def test_empty_group(self):
        engine = DefaultGroupEngine({"empty": set()})
        assert engine.is_member("anyone", "empty") is False

    def test_is_member_string_ids(self):
        """Groups work with any string — node_ids, skill names, anything."""
        engine = DefaultGroupEngine({
            "gpu_bundle": {"compute/gpu/inference", "compute/gpu/training"},
        })
        assert engine.is_member("compute/gpu/inference", "gpu_bundle") is True
        assert engine.is_member("compute/cpu/basic", "gpu_bundle") is False
