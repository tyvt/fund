# -*- coding: utf-8 -*-
"""A 股股息红利个人所得税（持股期限差异化税率）。"""

from __future__ import annotations

import pandas as pd

from dividend_lowvol_rotation.config import (
    DIVIDEND_TAX_MONTH_DAYS,
    DIVIDEND_TAX_YEAR_DAYS,
)


def dividend_tax_rate(hold_days: int) -> float:
    """按持股天数返回实际税负率（0 / 0.10 / 0.20）。

    - ≤1 个月：20%
    - 1 个月～1 年：10%
    - >1 年：0%
    """
    if hold_days <= DIVIDEND_TAX_MONTH_DAYS:
        return 0.20
    if hold_days <= DIVIDEND_TAX_YEAR_DAYS:
        return 0.10
    return 0.0


def dividend_tax_tier(hold_days: int) -> str:
    if hold_days <= DIVIDEND_TAX_MONTH_DAYS:
        return "≤1月(20%)"
    if hold_days <= DIVIDEND_TAX_YEAR_DAYS:
        return "1月~1年(10%)"
    return ">1年(0%)"


def build_dividend_index(records: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """按股票代码索引已实施分红记录（按除权日排序）。"""
    if records is None or records.empty:
        return {}
    sub = records.dropna(subset=["code", "ex_date", "cash_per_share"]).copy()
    sub["ex_date"] = pd.to_datetime(sub["ex_date"])
    sub = sub[sub["cash_per_share"] > 0].sort_values("ex_date")
    return {str(code): grp.reset_index(drop=True) for code, grp in sub.groupby("code")}


def accrue_dividend_taxes(
    lots: dict,
    div_index: dict[str, pd.DataFrame],
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
) -> tuple[float, float, list[dict]]:
    """统计区间内持仓现金分红与个税。

    返回 (总税额, 税前分红总额, 明细行)。调用方按回测模式处理现金：
    - 不复权 + 现金分红：cash += gross - tax（或 gross，若不扣税）
    - 前复权（旧模式）：仅 cash -= tax，避免与前复权价双重计入分红
    """
    total_tax = 0.0
    total_gross = 0.0
    rows: list[dict] = []

    for code, lot in lots.items():
        divs = div_index.get(code)
        if divs is None or divs.empty:
            continue
        mask = (divs["ex_date"] > period_start) & (divs["ex_date"] <= period_end)
        for _, div in divs.loc[mask].iterrows():
            ex = pd.Timestamp(div["ex_date"]).normalize()
            buy = pd.Timestamp(lot.buy_date).normalize()
            if buy >= ex:
                continue
            hold_days = (ex - buy).days
            rate = dividend_tax_rate(hold_days)
            gross = float(div["cash_per_share"]) * int(lot.shares)
            tax = gross * rate
            total_gross += gross
            total_tax += tax
            rows.append(
                {
                    "ex_date": ex.date().isoformat(),
                    "code": code,
                    "name": getattr(lot, "name", ""),
                    "shares": int(lot.shares),
                    "cash_per_share": round(float(div["cash_per_share"]), 4),
                    "gross_dividend": round(gross, 2),
                    "hold_days": hold_days,
                    "tax_tier": dividend_tax_tier(hold_days),
                    "tax_rate_pct": round(rate * 100, 2),
                    "tax_amount": round(tax, 2),
                    "net_dividend": round(gross - tax, 2),
                }
            )
    return total_tax, total_gross, rows
