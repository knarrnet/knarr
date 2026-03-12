"""
Netting policy — A5.5.

Determines when the netting cycle should auto-initiate for a peer.

Two built-in policies:
    RatioPolicy   — fires when abs(balance)/abs(hard_limit) >= threshold
    ManualPolicy  — never auto-fires (operator triggers manually)

Config layout:
    [netting]
    policy = "ratio"   # or "manual"

    [netting.ratio]
    threshold = 0.85
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from typing import Any, Dict

logger = logging.getLogger(__name__)


class NettingPolicy(ABC):
    """Abstract netting trigger policy."""

    @abstractmethod
    def should_initiate(self, balance: float, hard_limit: float) -> bool:
        """Return True if netting should be initiated for this peer position.

        Args:
            balance:    Current bilateral balance (negative = they owe us).
            hard_limit: Hard credit limit (must be negative for credit relationships).

        Returns:
            True if netting should fire.
        """


class RatioPolicy(NettingPolicy):
    """Fire when abs(balance)/abs(hard_limit) >= threshold.

    Default threshold = 0.85 (85% utilization).
    """

    def __init__(self, threshold: float = 0.85) -> None:
        if not math.isfinite(threshold) or not (0.0 < threshold <= 1.0):
            raise ValueError(
                f"RatioPolicy threshold must be in (0, 1], got {threshold!r}"
            )
        self.threshold = threshold

    def should_initiate(self, balance: float, hard_limit: float) -> bool:
        if not math.isfinite(balance) or not math.isfinite(hard_limit):
            return False
        if hard_limit >= 0:
            # No negative credit limit — no credit relationship
            return False
        utilization = abs(min(balance, 0.0)) / abs(hard_limit)
        return utilization >= self.threshold


class ManualPolicy(NettingPolicy):
    """Never auto-fires.  Operator must trigger netting manually."""

    def should_initiate(self, balance: float, hard_limit: float) -> bool:
        return False


def get_policy(config: Dict[str, Any]) -> NettingPolicy:
    """Build and return the configured netting policy.

    Args:
        config: Full node config dict.

    Returns:
        A NettingPolicy instance.
    """
    netting_cfg = config.get("netting", {})
    policy_name = str(netting_cfg.get("policy", "ratio")).strip().lower()

    if policy_name == "manual":
        logger.debug("NETTING_POLICY selected=ManualPolicy")
        return ManualPolicy()

    if policy_name == "ratio":
        ratio_cfg = netting_cfg.get("ratio", {})
        threshold = float(ratio_cfg.get("threshold", 0.85))
        logger.debug(f"NETTING_POLICY selected=RatioPolicy threshold={threshold}")
        return RatioPolicy(threshold=threshold)

    logger.warning(
        f"NETTING_POLICY unknown policy={policy_name!r} — defaulting to RatioPolicy(0.85)"
    )
    return RatioPolicy()
