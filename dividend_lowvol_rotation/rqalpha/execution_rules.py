# -*- coding: utf-8 -*-
"""RQAlpha 执行规则：与 backtest.py 对齐的最短持有期等。"""

from __future__ import annotations

from dividend_lowvol_rotation.config import (
    BACKTEST_MIN_HOLD_DAYS,
    BACKTEST_REBALANCE_MODE,
    DIVIDEND_TAX_YEAR_DAYS,
)
from dividend_lowvol_rotation.rqalpha.bridge import RebalancePlan


def resolve_min_hold_days(
    rebalance_mode: str | None = None,
    explicit: int | None = None,
) -> int:
    """与 backtest._resolve_min_hold_days 一致。"""
    if explicit is not None:
        return max(0, int(explicit))
    mode = (rebalance_mode or BACKTEST_REBALANCE_MODE).lower()
    if mode == "index_annual":
        return DIVIDEND_TAX_YEAR_DAYS
    return BACKTEST_MIN_HOLD_DAYS


def hold_days_since(buy_date, as_of) -> int:
    if buy_date is None:
        return 0
    return (as_of.normalize() - buy_date.normalize()).days


def apply_min_hold_to_plan(
    plan: RebalancePlan,
    *,
    buy_dates: dict[str, object],
    as_of,
    min_hold_days: int,
    current_weights: dict[str, float],
) -> RebalancePlan:
    """未满最短持有期的持仓：禁止卖出、禁止减持（与 backtest 一致）。"""
    if min_hold_days <= 0:
        return plan

    kept_sells: list[str] = []
    skipped_sells = 0
    for code in plan.sell_codes:
        days = hold_days_since(buy_dates.get(code), as_of)
        if days >= min_hold_days:
            kept_sells.append(code)
        else:
            skipped_sells += 1
    plan.sell_codes = kept_sells

    adjusted: dict[str, float] = {}
    skipped_trims = 0
    for code, target_w in plan.weights.items():
        days = hold_days_since(buy_dates.get(code), as_of)
        cur_w = current_weights.get(code, 0.0)
        if days < min_hold_days and target_w < cur_w - 1e-9:
            adjusted[code] = cur_w
            skipped_trims += 1
        else:
            adjusted[code] = target_w
    plan.weights = adjusted

    if skipped_sells or skipped_trims:
        plan.notes.append(
            f"最短持有 {min_hold_days} 天：跳过卖出 {skipped_sells} 只、跳过减持 {skipped_trims} 只"
        )
    return plan
