"""年度总投入预算：按年动态缩放各指数基准买入金额（保持利润最大化比例）。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from buy_amount_tiers import estimate_avg_multiplier, resolve_tiered_amount


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


def get_annual_budget(year: int | None = None) -> float:
    """指定年份的年度总投入预算（元）。"""
    from config import (
        ANNUAL_INVESTMENT_BUDGET,
        ANNUAL_INVESTMENT_BUDGET_BY_YEAR,
    )

    y = year if year is not None else date.today().year
    if y in ANNUAL_INVESTMENT_BUDGET_BY_YEAR:
        return float(ANNUAL_INVESTMENT_BUDGET_BY_YEAR[y])
    return float(ANNUAL_INVESTMENT_BUDGET)


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
    budget = get_annual_budget(y)
    scale = annual_budget_scale(y)
    return f"年度总投入 **{budget:,.0f}** 元（{y}，缩放 **{scale:.2f}×**）"


def _tier_scale_for_year(
    panel,
    year: int,
    buy_fn,
    tier_scheme,
    date_col="date",
) -> float:
    """按自然年买入日计算分档归一化系数，与年度回测一致。"""
    avg = estimate_avg_multiplier(
        panel,
        f"{year}-01-01",
        f"{year}-12-31",
        buy_fn,
        tier_scheme,
        date_col,
    )
    return 1.0 / avg if avg > 0 else 1.0


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
    """回测用：按买入日年份动态计算分档金额。"""
    tier_scheme = amounts.get("tier_scheme") if amounts else None
    tier_normalize = bool(amounts and amounts.get("tier_normalize"))
    tier_scale_cache: dict[int, float] = {}

    def _tier_scale(year: int) -> float:
        if not tier_scheme or not tier_normalize:
            return 1.0
        if year not in tier_scale_cache:
            tier_scale_cache[year] = _tier_scale_for_year(
                panel, year, buy_fn, tier_scheme, date_col
            )
        return tier_scale_cache[year]

    def _fn(row):
        year = _row_year(row, date_col)
        base = scale_base_by_annual_budget(reference_base, year) * _tier_scale(year)
        if tier_scheme:
            return resolve_tiered_amount(base, row, tier_scheme)
        return base

    return _fn
