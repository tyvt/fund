"""估值 + MA200 破位分批卖出，以及 252 日回撤买入倍数。

日报只判当日条件；跨日「同档只减一次」由回测状态机记录。
"""

from __future__ import annotations

import math

TIER_VALUATION = "valuation"
TIER_TREND = "trend"
TIER_EXTREME = "extreme"

_TIER_FRACTION = {
    TIER_VALUATION: 1 / 3,
    TIER_TREND: 1 / 3,
}


def _num(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _flag(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    try:
        if math.isnan(float(value)):
            return False
    except (TypeError, ValueError):
        return bool(value)
    return bool(value)


def active_sell_tiers(
    *,
    pe_percentile,
    spread_percentile=None,
    below_ma=None,
    ma_slope_pct=None,
    is_buy=False,
    pe_high=80.0,
    pe_extreme=90.0,
    spread_low=20.0,
    spread_extreme=10.0,
    slope_max=-0.02,
):
    """当日触发的卖出档。买入日返回空。极端档覆盖前两档。"""
    if is_buy:
        return []
    pe = _num(pe_percentile)
    spread = _num(spread_percentile)
    slope = _num(ma_slope_pct)
    extreme = (pe is not None and pe >= pe_extreme) or (
        spread is not None and spread <= spread_extreme
    )
    if extreme:
        return [TIER_EXTREME]
    tiers = []
    if (pe is not None and pe >= pe_high) or (
        spread is not None and spread <= spread_low
    ):
        tiers.append(TIER_VALUATION)
    if _flag(below_ma) and slope is not None and slope < slope_max:
        tiers.append(TIER_TREND)
    return tiers


def units_for_new_tiers(tiers, already, units, initial_units):
    """按尚未触发的档计算本次卖出份额。返回 (units_to_sell, full_exit, newly_marked)。"""
    pending = [tier for tier in tiers if tier not in already]
    if not pending or units <= 0 or initial_units <= 0:
        return 0.0, False, []
    if TIER_EXTREME in pending:
        return float(units), True, [TIER_EXTREME, TIER_VALUATION, TIER_TREND]
    fraction = sum(_TIER_FRACTION[tier] for tier in pending if tier in _TIER_FRACTION)
    sell_units = min(float(units), float(initial_units) * fraction)
    if sell_units <= 0:
        return 0.0, False, []
    full = sell_units >= float(units) - 1e-12
    return sell_units, full, list(pending)


def drawdown_amount_multiplier(pct_below_high):
    """252 日高点回撤：<25% 为 1 倍，25%–35% 为 1.5 倍，≥35% 为 2 倍。"""
    drawdown = _num(pct_below_high)
    if drawdown is None:
        return 1.0
    if drawdown >= 0.35:
        return 2.0
    if drawdown >= 0.25:
        return 1.5
    return 1.0
