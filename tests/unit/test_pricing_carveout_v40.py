import sys
from pathlib import Path
import logging
import sqlite3
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import MagicMock

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

import knarr  # noqa: E402

knarr.__path__.insert(0, str(BASE_DIR / "src" / "knarr"))
sys.modules.pop("knarr.dht", None)
sys.modules.pop("knarr.commerce", None)

import knarr.commerce  # noqa: E402

knarr.commerce.__path__.insert(0, str(BASE_DIR / "src" / "knarr" / "commerce"))
sys.modules.pop("knarr.commerce.pricing_engine", None)
sys.modules.pop("knarr.dht.node", None)

from knarr.commerce import pricing_engine
from knarr.commerce.pricing_engine import PricingConfig, PricingResult
from knarr.dht.node import DHTNode


def _merge_dicts(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _make_pricing_node(
    *,
    config: dict | None = None,
    discounts: list[tuple[str, str, str, float, int]] | None = None,
    cost_projection: float | None = None,
    groups: tuple[str, ...] = ("vip",),
    skill_name: str = "echo",
):
    node = DHTNode.__new__(DHTNode)
    node._config = _merge_dicts(
        {
            "pricing": {
                "discount_mode": "multiplicative",
                "min_price": 0.01,
                "caps": {"*": 100.0},
                "floors": {"markup_minimum": 1.1},
            },
            "skills": {},
        },
        config or {},
    )
    node._group_engine = SimpleNamespace(get_groups=lambda _node_id: list(groups))

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE pricing_discounts (
            name TEXT,
            group_name TEXT,
            skill_group TEXT,
            effect_pct REAL,
            priority INTEGER,
            active INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE skill_cost_projection (
            skill_name TEXT,
            total_cost REAL
        )
        """
    )
    for row in discounts or []:
        conn.execute(
            "INSERT INTO pricing_discounts (name, group_name, skill_group, effect_pct, priority, active) VALUES (?, ?, ?, ?, ?, 1)",
            row,
        )
    if cost_projection is not None:
        conn.execute(
            "INSERT INTO skill_cost_projection (skill_name, total_cost) VALUES (?, ?)",
            (skill_name, cost_projection),
        )
    conn.commit()

    node.storage = SimpleNamespace(
        _get_conn=lambda: conn,
        get_discount_rules=lambda *a, **kw: [],
        get_cost_projection=lambda *a, **kw: None,
        get_execution_price=lambda *a, **kw: None,
    )
    return node


def _resolve(node, engine: str, base_price: float = 10.0, skill_name: str = "echo"):
    node._config.setdefault("pricing", {})["engine"] = engine
    return DHTNode._resolve_price(node, "peer-1", base_price, skill_name)


def _assert_same_result(node, *, base_price: float = 10.0, skill_name: str = "echo"):
    builtin_price, builtin_breakdown = _resolve(node, "builtin", base_price, skill_name)
    module_price, module_breakdown = _resolve(node, "module", base_price, skill_name)

    assert module_price == builtin_price
    assert asdict(module_breakdown) == asdict(builtin_breakdown)


def test_builtin_is_default():
    node = SimpleNamespace(
        _config={},
        _resolve_price_builtin=MagicMock(return_value=(1.25, "builtin")),
    )

    result = DHTNode._resolve_price(node, "peer-1", 3.0, "echo")

    assert result == (1.25, "builtin")
    node._resolve_price_builtin.assert_called_once_with("peer-1", 3.0, "echo")


def test_module_path_when_configured(monkeypatch):
    fake_result = PricingResult(
        final_price=2.5,
        base_price=3.0,
        cost_projection=None,
        rules_applied=[],
        discount_mode="multiplicative",
        floor_price=0.01,
        floor_applied=False,
        cap_applied=False,
    )
    resolver = MagicMock(return_value=fake_result)
    monkeypatch.setattr(pricing_engine, "resolve_price", resolver)

    node = DHTNode.__new__(DHTNode)
    node._config = {"pricing": {"engine": "module"}}
    node._group_engine = None
    node._load_discount_rules = MagicMock(return_value=[])
    node._get_cost_projection = MagicMock(return_value=None)
    node._get_skill_min_price = MagicMock(return_value=None)
    node._build_pricing_config = MagicMock(return_value=PricingConfig())
    node._pricing_result_to_breakdown = MagicMock(return_value="breakdown")
    node._resolve_price_builtin = MagicMock(side_effect=AssertionError("builtin should not run"))

    price, breakdown = DHTNode._resolve_price(node, "peer-1", 3.0, "echo")

    assert price == 2.5
    assert breakdown == "breakdown"
    resolver.assert_called_once()


def test_unknown_engine_fallback(caplog):
    node = SimpleNamespace(
        _config={"pricing": {"engine": "mystery"}},
        _resolve_price_builtin=MagicMock(return_value=(4.0, "builtin")),
    )

    with caplog.at_level(logging.WARNING):
        result = DHTNode._resolve_price(node, "peer-1", 5.0, "echo")

    assert result == (4.0, "builtin")
    node._resolve_price_builtin.assert_called_once_with("peer-1", 5.0, "echo")
    assert any("PRICING_ENGINE_UNKNOWN" in record.message for record in caplog.records)


def test_comparison_no_discounts():
    node = _make_pricing_node()
    _assert_same_result(node, base_price=10.0)


def test_comparison_multiplicative():
    node = _make_pricing_node(
        config={"pricing": {"discount_mode": "multiplicative"}},
        discounts=[
            ("vip-25", "vip", "*", 25.0, 10),
            ("vip-10", "vip", "*", 10.0, 5),
        ],
    )
    _assert_same_result(node, base_price=10.0)


def test_comparison_additive():
    node = _make_pricing_node(
        config={"pricing": {"discount_mode": "additive"}},
        discounts=[
            ("vip-20", "vip", "*", 20.0, 10),
            ("vip-15", "vip", "*", 15.0, 5),
        ],
    )
    _assert_same_result(node, base_price=10.0)


def test_comparison_best_wins():
    node = _make_pricing_node(
        config={"pricing": {"discount_mode": "best_wins"}},
        discounts=[
            ("vip-10", "vip", "*", 10.0, 10),
            ("vip-35", "vip", "*", 35.0, 5),
            ("vip-20", "vip", "*", 20.0, 1),
        ],
    )
    _assert_same_result(node, base_price=10.0)


def test_comparison_floor_clamping():
    node = _make_pricing_node(
        config={"pricing": {"floors": {"markup_minimum": 1.2}}},
        cost_projection=5.0,
    )
    _assert_same_result(node, base_price=1.0)


def test_comparison_global_minimum():
    node = _make_pricing_node(
        config={"skills": {"minimum_price": 2.5}},
    )
    _assert_same_result(node, base_price=1.0)
