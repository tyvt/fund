"""Deterministic incremental-capital deployment rules."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np


def _holding_value(value: Any) -> float:
    if isinstance(value, Mapping):
        value = value.get("market_value", value.get("value", 0.0))
    number = float(value)
    return number if np.isfinite(number) and number > 0 else 0.0


def deploy_new_capital(
    current_portfolio: Mapping[str, Any],
    target_positions: Sequence[Any],
    new_capital: float,
    max_daily_trade: float = 0.20,
    *,
    market_overvalued: bool = False,
) -> dict[str, Any]:
    """Create one day's buy plan while preserving undeployed capital as cash.

    Existing target holdings are filled first in descending score order, then
    new candidates.  Each target may provide ``symbol``, ``score`` and
    ``target_weight``; string targets receive equal weight and neutral score.
    """
    capital = float(new_capital)
    daily_ratio = float(max_daily_trade)
    if not np.isfinite(capital) or capital < 0:
        raise ValueError("new_capital must be finite and non-negative")
    if not 0 < daily_ratio <= 1:
        raise ValueError("max_daily_trade must be in (0, 1]")
    current = {str(symbol): _holding_value(value) for symbol, value in current_portfolio.items()}
    current_value = sum(current.values())
    total_after_injection = current_value + capital
    daily_limit = min(capital, total_after_injection * daily_ratio)
    if market_overvalued or capital == 0:
        return {
            "orders": [], "invested": 0.0, "cash_remaining": capital,
            "daily_limit": daily_limit, "market_overvalued": bool(market_overvalued),
        }

    count = len(target_positions)
    normalized = []
    for item in target_positions:
        if isinstance(item, Mapping):
            symbol = str(item.get("symbol", item.get("code")))
            score = float(item.get("score", 0.0))
            target_weight = float(item.get("target_weight", 1.0 / max(count, 1)))
        else:
            symbol, score = str(item), 0.0
            target_weight = 1.0 / max(count, 1)
        if symbol in {"", "None"} or target_weight < 0:
            raise ValueError("invalid target position")
        normalized.append(
            {"symbol": symbol, "score": score, "target_weight": target_weight}
        )
    weight_sum = sum(item["target_weight"] for item in normalized)
    if weight_sum <= 0 and normalized:
        raise ValueError("target weights must sum to a positive value")
    for item in normalized:
        item["target_weight"] /= weight_sum
        item["incumbent"] = item["symbol"] in current
    normalized.sort(key=lambda item: (not item["incumbent"], -item["score"], item["symbol"]))

    remaining = daily_limit
    orders = []
    for item in normalized:
        target_value = total_after_injection * item["target_weight"]
        underweight = max(0.0, target_value - current.get(item["symbol"], 0.0))
        amount = min(underweight, remaining)
        if amount > 1e-8:
            orders.append(
                {
                    "symbol": item["symbol"],
                    "amount": float(amount),
                    "score": float(item["score"]),
                    "incumbent": bool(item["incumbent"]),
                }
            )
            remaining -= amount
        if remaining <= 1e-8:
            break
    invested = daily_limit - remaining
    return {
        "orders": orders,
        "invested": float(invested),
        "cash_remaining": float(capital - invested),
        "daily_limit": float(daily_limit),
        "market_overvalued": False,
    }


__all__ = ["deploy_new_capital"]
