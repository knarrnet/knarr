"""
Two-layer conversion — A2.

Token/credit boundary functions.  Only prepaid and settlement amounts
cross the boundary.  Prices, limits, pub_tab stay in credits.

Worked example (rate = 1.0, devnet):
    Deposit:     100 $KNARR on-chain → token_to_credits(100, 1.0) = 100.0 credits
    Settlement:  42.0 credits owed   → credits_to_token(42.0, 1.0) = 42.0 $KNARR

Config layout:
    [economy]
    conversion_rate = 1.0
    rate_source     = "config"   # "config" | "oracle" (oracle not implemented yet)
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict

logger = logging.getLogger(__name__)

_DEFAULT_RATE = 1.0


def get_conversion_rate(config: Dict[str, Any]) -> float:
    """Return the active token-to-credits conversion rate.

    Only "config" rate_source is supported.  Returns default 1.0 for devnet.

    Args:
        config: Full node config dict.

    Returns:
        Positive finite float conversion rate.

    Raises:
        ValueError: If the configured rate is not positive or not finite.
    """
    economy_cfg = config.get("economy", {})
    rate_source = str(economy_cfg.get("rate_source", "config")).strip()

    if rate_source != "config":
        logger.warning(
            f"CONVERSION_RATE unsupported rate_source={rate_source!r} "
            f"— falling back to config"
        )

    raw = economy_cfg.get("conversion_rate", _DEFAULT_RATE)
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"conversion_rate must be numeric, got {raw!r}"
        )

    if not math.isfinite(rate):
        raise ValueError(f"conversion_rate must be finite, got {rate!r}")
    if rate <= 0:
        raise ValueError(f"conversion_rate must be positive, got {rate!r}")

    return rate


def token_to_credits(amount: float, rate: float) -> float:
    """Convert on-chain token amount to ledger credits (deposit boundary).

    Args:
        amount: Token amount (must be positive finite).
        rate:   Conversion rate (must be positive finite).

    Returns:
        Credit amount as positive finite float.

    Raises:
        ValueError: On NaN, Inf, zero, or negative inputs.
    """
    _validate_amount(amount, "amount")
    _validate_rate(rate)
    result = amount * rate
    if not math.isfinite(result):
        raise ValueError(
            f"token_to_credits overflow: amount={amount} rate={rate} result={result}"
        )
    return result


def credits_to_token(amount: float, rate: float) -> float:
    """Convert ledger credits to on-chain token amount (withdrawal boundary).

    Args:
        amount: Credit amount (must be positive finite).
        rate:   Conversion rate (must be positive finite).

    Returns:
        Token amount as positive finite float.

    Raises:
        ValueError: On NaN, Inf, zero, or negative inputs.
    """
    _validate_amount(amount, "amount")
    _validate_rate(rate)
    result = amount / rate
    if not math.isfinite(result):
        raise ValueError(
            f"credits_to_token overflow: amount={amount} rate={rate} result={result}"
        )
    return result


# ── Internal helpers ───────────────────────────────────────────────────


def _validate_amount(value: float, name: str) -> None:
    """Raise ValueError if value is not a positive finite number."""
    if not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric, got {type(value).__name__}")
    fv = float(value)
    if not math.isfinite(fv):
        raise ValueError(f"{name} must be finite, got {fv!r}")
    if fv <= 0:
        raise ValueError(f"{name} must be positive, got {fv!r}")


def _validate_rate(rate: float) -> None:
    """Raise ValueError if rate is not a positive finite number."""
    if not isinstance(rate, (int, float)):
        raise ValueError(f"rate must be numeric, got {type(rate).__name__}")
    fr = float(rate)
    if not math.isfinite(fr):
        raise ValueError(f"rate must be finite, got {fr!r}")
    if fr <= 0:
        raise ValueError(f"rate must be positive, got {fr!r}")
