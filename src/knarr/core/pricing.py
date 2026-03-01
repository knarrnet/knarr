"""Structured pricing engine and meta-realm config for knarr skill execution."""
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class RealmConfig:
    """Configuration for a meta cache realm."""
    queries: List[str]
    access: Dict[str, str]  # query_name -> "public" | "authenticated" | "caller_only"
    refresh_cb: Optional[Any] = None
    handler: Optional[Any] = None  # v0.32.0: dynamic handler for non-cacheable queries


@dataclass
class PriceBreakdown:
    base_price: float
    cost_projection: Optional[float]
    rules_applied: List[Dict[str, Any]]
    discount_mode: str
    floor_price: float
    floor_applied: bool
    promotion_applied: bool
    final_price: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(',', ':'))
