"""轮动卖出门控：仅在存在其他指数买点、且边际收益不足时执行估值类卖出。"""

from __future__ import annotations

from config import ROTATION_MARGINAL_HURDLE_ANN_PCT


def annualized_position_return_pct(
    price: float,
    avg_cost: float,
    days_since_buy: int | None,
) -> float | None:
    """持仓年化浮盈（%，粗算），用于判断继续持有一年是否「划算」。"""
    if (
        avg_cost is None
        or avg_cost <= 0
        or days_since_buy is None
        or days_since_buy <= 0
    ):
        return None
    gain = (price - avg_cost) / avg_cost
    years = days_since_buy / 365.25
    if years <= 0 or (1 + gain) <= 0:
        return None
    return ((1 + gain) ** (1 / years) - 1) * 100


def other_index_buy_today(code: str, buy_flags: dict[str, bool]) -> bool:
    """当日是否有其他指数触发买入信号。"""
    return any(flag for c, flag in buy_flags.items() if c != code and flag)


def rotation_sell_allowed(
    code: str,
    buy_flags: dict[str, bool],
    *,
    is_trailing: bool,
    is_valuation: bool,
    annualized_gain_pct: float | None,
    hurdle_ann_pct: float | None = None,
) -> bool:
    """轮动门控：必须有其他指数买点；估值卖还需持仓年化低于门槛。"""
    if not other_index_buy_today(code, buy_flags):
        return False
    if is_trailing:
        return True
    if is_valuation:
        hurdle = (
            ROTATION_MARGINAL_HURDLE_ANN_PCT
            if hurdle_ann_pct is None
            else hurdle_ann_pct
        )
        if annualized_gain_pct is None:
            return True
        return annualized_gain_pct < hurdle
    return False
