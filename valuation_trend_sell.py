"""回撤买入倍数（252 日高点回撤）。

旧「估值 + MA200 破位分批卖出」逻辑已删除（A 方案：三指数统一为单一粗移动止盈）。
本模块仅保留买入端使用的 ``drawdown_amount_multiplier``。
"""

from __future__ import annotations

import math


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
