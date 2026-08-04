"""投入预算：剩余可用额度、预计全年投入与涨跌缩放。"""

from __future__ import annotations

import calendar
from datetime import date

import pandas as pd


def year_time_info(as_of: date | None = None) -> dict:
    """自然年已过/剩余比例（按日历日近似）。"""
    today = as_of or date.today()
    days_in_year = 366 if calendar.isleap(today.year) else 365
    day_of_year = today.timetuple().tm_yday
    days_elapsed = max(day_of_year - 1, 0)
    days_remaining = max(days_in_year - day_of_year + 1, 1)
    return {
        "year": today.year,
        "days_in_year": days_in_year,
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "fraction_elapsed": days_elapsed / days_in_year,
        "fraction_remaining": days_remaining / days_in_year,
    }


def estimate_annual_investment(
    remaining_budget: float | None = None,
    *,
    as_of: date | None = None,
) -> float:
    """由剩余可用额度按剩余时间比例外推预计全年投入。"""
    if remaining_budget is None:
        from config import REMAINING_INVESTMENT_BUDGET

        remaining_budget = REMAINING_INVESTMENT_BUDGET
    info = year_time_info(as_of)
    frac = info["fraction_remaining"]
    if frac <= 0:
        return float(remaining_budget)
    return float(remaining_budget) / frac


def _row_year(row, date_col="date") -> int:
    if hasattr(row, "get"):
        raw = row.get(date_col) or row.get("date_only")
    else:
        raw = getattr(row, date_col, None) or getattr(row, "date_only", None)
    if raw is None:
        return date.today().year
    return pd.Timestamp(raw).year


def is_annual_budget_enabled(amounts=None) -> bool:
    from config import ANNUAL_INVESTMENT_BUDGET_ENABLED

    if amounts is not None and "annual_budget" in amounts:
        return bool(amounts["annual_budget"])
    return ANNUAL_INVESTMENT_BUDGET_ENABLED


def get_remaining_investment_budget(year: int | None = None) -> float:
    """当年剩余可用投入额度（元）；历史回测年份返回预计/配置全年额度。"""
    from config import ANNUAL_INVESTMENT_BUDGET_BY_YEAR, REMAINING_INVESTMENT_BUDGET

    y = year if year is not None else date.today().year
    if y in ANNUAL_INVESTMENT_BUDGET_BY_YEAR:
        return float(ANNUAL_INVESTMENT_BUDGET_BY_YEAR[y])
    if y != date.today().year:
        return get_annual_budget(y)
    return float(REMAINING_INVESTMENT_BUDGET)


def get_annual_budget(year: int | None = None) -> float:
    """指定年份的预计/配置全年投入（元）。"""
    from config import ANNUAL_INVESTMENT_BUDGET_BY_YEAR, ANNUAL_INVESTMENT_TARGET

    y = year if year is not None else date.today().year
    if y in ANNUAL_INVESTMENT_BUDGET_BY_YEAR:
        return float(ANNUAL_INVESTMENT_BUDGET_BY_YEAR[y])
    if ANNUAL_INVESTMENT_TARGET > 0:
        return float(ANNUAL_INVESTMENT_TARGET)
    if y < date.today().year:
        return estimate_annual_investment(72_000, as_of=date(y, 6, 30))
    return estimate_annual_investment()


def annual_budget_scale(year: int | None = None) -> float:
    """相对参考年度预算的缩放系数。"""
    from config import BUY_AMOUNT_REFERENCE_ANNUAL_BUDGET

    ref = BUY_AMOUNT_REFERENCE_ANNUAL_BUDGET
    if ref <= 0:
        return 1.0
    return get_annual_budget(year) / ref


def scale_base_by_annual_budget(base_amount: float, year: int | None = None) -> float:
    """将参考基准金额按年度预算缩放。"""
    if base_amount <= 0:
        return 0.0
    return max(10.0, round(base_amount * annual_budget_scale(year)))


def get_scaled_buy_amount_base(index_code: str, year: int | None = None) -> float:
    from config import get_buy_amount_reference

    ref = get_buy_amount_reference(index_code)
    if not is_annual_budget_enabled():
        return ref
    return scale_base_by_annual_budget(ref, year)


def format_annual_budget_note(year: int | None = None) -> str:
    y = year if year is not None else date.today().year
    remaining = get_remaining_investment_budget(y)
    estimated = get_annual_budget(y)
    if y == date.today().year:
        return (
            f"剩余可用 **{remaining:,.0f}** 元（{y}）；"
            f"预计全年投入 **{estimated:,.0f}** 元"
        )
    return f"年度总投入 **{estimated:,.0f}** 元（{y}）"


def make_annual_amount_fn(
    code: str,
    reference_base: float,
    amounts,
    panel,
    start_date,
    end_date,
    buy_fn,
    date_col="date",
):
    """回测用：按买入日年份缩放基准，再按当日涨跌比例调整。"""
    from buy_amount_change import change_multiplier, row_daily_change_pct
    from config import BUY_AMOUNT_CHANGE_SCALE_ENABLED

    change_scale = (
        amounts.get("change_scale", BUY_AMOUNT_CHANGE_SCALE_ENABLED)
        if amounts
        else BUY_AMOUNT_CHANGE_SCALE_ENABLED
    )

    change_by_date: dict[str, float] = {}
    if change_scale and panel is not None and not panel.empty:
        work = panel.copy()
        if date_col in work.columns:
            work = work.sort_values(date_col)
        closes = pd.to_numeric(work.get("close"), errors="coerce")
        if closes is not None:
            changes = closes.pct_change()
            for dt, chg in zip(work[date_col], changes):
                if pd.isna(chg):
                    continue
                change_by_date[pd.Timestamp(dt).strftime("%Y-%m-%d")] = float(chg)

    def _fn(row):
        year = _row_year(row, date_col)
        base = scale_base_by_annual_budget(reference_base, year)
        if not change_scale:
            return base
        from sell_trailing import row_field

        chg = None
        raw = row_field(row, date_col) or row_field(row, "date_only")
        if raw is not None:
            chg = change_by_date.get(pd.Timestamp(raw).strftime("%Y-%m-%d"))
        if chg is None:
            chg = row_daily_change_pct(row)
        return max(10.0, round(base * change_multiplier(chg)))

    return _fn
