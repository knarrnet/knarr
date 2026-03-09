"""Tests for v0.41.0 B2: Pricing Parity — FINDING-G.

base_price=0.0 must return effective_price=0.0 via BOTH pricing paths,
regardless of skill_floor, global_min, or surcharge rules.
"""
from unittest.mock import MagicMock

import pytest

from knarr.commerce.pricing_engine import (
    PricingConfig,
    PricingRequest,
    DiscountRule,
    resolve_price,
)


class TestModulePath:
    """Tests for the module pricing engine (pricing_engine.py)."""

    def test_base_price_zero_returns_zero(self):
        """base_price=0.0 returns effective_price=0.0 via module path."""
        req = PricingRequest(base_price=0.0, skill_name="free-skill", peer_node_id="abc")
        config = PricingConfig()
        result = resolve_price(req, config)
        assert result.final_price == 0.0
        assert result.floor_applied is False

    def test_base_price_zero_with_skill_floor(self):
        """base_price=0.0 with skill_floor > 0 still returns 0.0 via module."""
        req = PricingRequest(
            base_price=0.0, skill_name="free-skill", peer_node_id="abc",
            skill_min_price=0.5,
        )
        config = PricingConfig(min_price=0.5, global_min_price=0.1)
        result = resolve_price(req, config)
        assert result.final_price == 0.0

    def test_base_price_zero_with_surcharge_rules(self):
        """base_price=0.0 with surcharge rules still returns 0.0."""
        req = PricingRequest(
            base_price=0.0, skill_name="free-skill", peer_node_id="abc",
            discount_rules=[
                DiscountRule(name="test", group_name="g", skill_group="*",
                             effect_pct=-20.0),  # negative = surcharge
            ],
        )
        config = PricingConfig()
        result = resolve_price(req, config)
        assert result.final_price == 0.0

    def test_nonzero_base_gets_normal_treatment(self):
        """base_price=0.01 still gets normal floor/surcharge treatment."""
        req = PricingRequest(
            base_price=0.01, skill_name="cheap-skill", peer_node_id="abc",
        )
        config = PricingConfig(min_price=0.01)
        result = resolve_price(req, config)
        assert result.final_price >= 0.01


class TestBuiltinPath:
    """Tests for the builtin pricing path (_resolve_price_builtin in node.py)."""

    def _make_node(self, config_overrides=None):
        """Build a minimal mock node for _resolve_price_builtin."""
        node = MagicMock()
        config = {
            "pricing": {
                "discount_mode": "multiplicative",
                "min_price": 0.01,
                "caps": {},
                "floors": {"markup_minimum": 1.1},
            },
            "skills": {"minimum_price": 0.0},
        }
        if config_overrides:
            for k, v in config_overrides.items():
                if isinstance(v, dict) and k in config:
                    config[k].update(v)
                else:
                    config[k] = v
        node._config = config
        node._group_engine = None
        node.storage._get_conn.return_value.execute.return_value.fetchall.return_value = []
        node.storage._get_conn.return_value.execute.return_value.fetchone.return_value = None
        return node

    def test_base_price_zero_returns_zero_builtin(self):
        """base_price=0.0 returns effective_price=0.0 via builtin path."""
        from knarr.dht.node import DHTNode

        node = self._make_node()
        price, breakdown = DHTNode._resolve_price_builtin(node, "abc123", 0.0, "free-skill")
        assert price == 0.0
        assert breakdown.final_price == 0.0
        assert breakdown.floor_applied is False

    def test_base_price_zero_with_skill_floor_builtin(self):
        """base_price=0.0 with skill_floor > 0 still returns 0.0 via builtin."""
        from knarr.dht.node import DHTNode

        node = self._make_node({"pricing": {"min_price": 0.5},
                                 "skills": {"free-skill": {"min_price": 0.5},
                                            "minimum_price": 0.1}})
        price, breakdown = DHTNode._resolve_price_builtin(node, "abc123", 0.0, "free-skill")
        assert price == 0.0
        assert breakdown.floor_applied is False

    def test_base_price_zero_with_global_min_builtin(self):
        """base_price=0.0 with global minimum_price > 0 still returns 0.0."""
        from knarr.dht.node import DHTNode

        node = self._make_node({"skills": {"minimum_price": 0.05}})
        price, breakdown = DHTNode._resolve_price_builtin(node, "abc123", 0.0, "free-skill")
        assert price == 0.0

    def test_nonzero_base_gets_floor_builtin(self):
        """base_price=0.01 still gets normal floor/surcharge treatment."""
        from knarr.dht.node import DHTNode

        node = self._make_node()
        price, breakdown = DHTNode._resolve_price_builtin(node, "abc123", 0.01, "paid-skill")
        assert price >= 0.01

    def test_parity_both_paths_zero(self):
        """Both paths return identical result for base_price=0.0."""
        from knarr.dht.node import DHTNode

        # Module path
        req = PricingRequest(base_price=0.0, skill_name="free", peer_node_id="abc")
        module_result = resolve_price(req, PricingConfig())

        # Builtin path
        node = self._make_node()
        builtin_price, builtin_bd = DHTNode._resolve_price_builtin(
            node, "abc", 0.0, "free"
        )

        assert module_result.final_price == builtin_price == 0.0
        assert module_result.floor_applied is False
        assert builtin_bd.floor_applied is False
