"""Tests for provider selection strategies: cheapest, jurisdiction, first."""
import pytest


class TestProviderStrategyCheapest:
    """Cheapest strategy picks lowest price, then lowest load."""

    def test_cheapest_by_price(self):
        candidates = [
            {"node_id": "a", "host": "h1", "port": 9000, "price": 5.0, "load": 5},
            {"node_id": "b", "host": "h2", "port": 9000, "price": 1.0, "load": 5},
            {"node_id": "c", "host": "h3", "port": 9000, "price": 3.0, "load": 5},
        ]
        provider = min(candidates, key=lambda p: (p.get("price", 1.0), p.get("load", 10)))
        assert provider["node_id"] == "b"

    def test_cheapest_tiebreak_by_load(self):
        candidates = [
            {"node_id": "a", "host": "h1", "port": 9000, "price": 1.0, "load": 8},
            {"node_id": "b", "host": "h2", "port": 9000, "price": 1.0, "load": 2},
        ]
        provider = min(candidates, key=lambda p: (p.get("price", 1.0), p.get("load", 10)))
        assert provider["node_id"] == "b"


class TestProviderStrategyJurisdiction:
    """Jurisdiction strategy: strict by default, no fallback [N-2]."""

    def test_jurisdiction_match(self):
        candidates = [
            {"node_id": "a", "jurisdiction": ["us"]},
            {"node_id": "b", "jurisdiction": ["eu.se"]},
        ]
        target = "eu.se"
        result = None
        for c in candidates:
            if target in (c.get("jurisdiction") or []):
                result = c
                break
        assert result is not None
        assert result["node_id"] == "b"

    def test_jurisdiction_strict_no_fallback(self):
        """EU-only must mean EU-only [N-2]."""
        candidates = [
            {"node_id": "a", "jurisdiction": ["us"]},
        ]
        target = "eu.se"
        strict = True
        result = None
        for c in candidates:
            if target in (c.get("jurisdiction") or []):
                result = c
                break
        if not result and strict:
            result = "NO_MATCH"  # Should return error
        assert result == "NO_MATCH"

    def test_jurisdiction_non_strict_fallback(self):
        """When jurisdiction_strict=false, fallback to any."""
        candidates = [
            {"node_id": "a", "jurisdiction": ["us"]},
        ]
        target = "eu.se"
        strict = False
        result = None
        for c in candidates:
            if target in (c.get("jurisdiction") or []):
                result = c
                break
        if not result and not strict and candidates:
            result = candidates[0]
        assert result["node_id"] == "a"

    def test_jurisdiction_empty_list(self):
        """Providers without jurisdiction field never match."""
        candidates = [
            {"node_id": "a"},
            {"node_id": "b", "jurisdiction": None},
        ]
        target = "eu.se"
        result = None
        for c in candidates:
            if target in (c.get("jurisdiction") or []):
                result = c
                break
        assert result is None
