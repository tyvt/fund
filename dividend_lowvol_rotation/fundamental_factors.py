# -*- coding: utf-8 -*-
"""预期股息率、质量动量（ROE 同比变化）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from dividend_lowvol_rotation.config import EXPECTED_DIVIDEND_LOOKBACK_YEARS
from dividend_lowvol_rotation.symbols import normalize_stock_code


def attach_expected_dividend_yield(
    panel: pd.DataFrame,
    records: pd.DataFrame,
    as_of: pd.Timestamp,
) -> pd.DataFrame:
    """预期股息率 ≈ 过去 N 年平均支付率(%) / PE；PE = price/eps。"""
    if panel.empty or records is None or records.empty:
        return panel
    out = panel.copy()
    rec = records.copy()
    rec["code"] = rec["code"].map(normalize_stock_code)
    rec["ex_date"] = pd.to_datetime(rec["ex_date"], errors="coerce")
    rec = rec[rec["ex_date"].notna() & (rec["ex_date"] <= as_of)]
    lookback = EXPECTED_DIVIDEND_LOOKBACK_YEARS

    exp_yields: list[float | None] = []
    avg_payouts: list[float | None] = []
    for _, row in out.iterrows():
        code = normalize_stock_code(str(row["code"]))
        price = pd.to_numeric(row.get("price"), errors="coerce")
        eps = pd.to_numeric(row.get("eps"), errors="coerce")
        sub = rec[rec["code"] == code]
        payout_years: list[float] = []
        if not sub.empty:
            for year in range(as_of.year - lookback + 1, as_of.year + 1):
                yr = sub[sub["ex_date"].dt.year == year]
                if yr.empty:
                    continue
                cash = pd.to_numeric(yr["cash_per_share"], errors="coerce").sum()
                eps_y = pd.to_numeric(yr["eps"], errors="coerce").dropna()
                if eps_y.empty:
                    continue
                eps_val = float(eps_y.iloc[-1])
                if eps_val > 0 and cash > 0:
                    payout_years.append(cash / eps_val * 100.0)
        avg_pay = float(np.mean(payout_years)) if payout_years else None
        avg_payouts.append(avg_pay)
        if avg_pay is not None and price and eps and eps > 0 and price > 0:
            pe = price / eps
            exp_yields.append(avg_pay / pe if pe > 0 else None)
        else:
            exp_yields.append(None)

    out["avg_payout_3y_pct"] = avg_payouts
    out["expected_div_yield_pct"] = exp_yields
    return out


def attach_quality_momentum(
    panel: pd.DataFrame,
    risk_hist: pd.DataFrame,
    as_of: pd.Timestamp,
) -> pd.DataFrame:
    """质量动量：最新年报 ROE − 上一年 ROE（百分点）。"""
    if panel.empty or risk_hist is None or risk_hist.empty:
        return panel
    out = panel.copy()
    sub = risk_hist.copy()
    sub["code"] = sub["code"].map(normalize_stock_code)
    sub = sub[sub["report_year"] <= as_of.year]
    mom_map: dict[str, float] = {}
    for code, grp in sub.groupby("code"):
        g = grp.sort_values("report_year")
        if len(g) < 2 or "roe_pct" not in g.columns:
            continue
        roe = pd.to_numeric(g["roe_pct"], errors="coerce")
        if roe.notna().sum() < 2:
            continue
        latest = float(roe.iloc[-1])
        prev = float(roe.iloc[-2])
        if np.isfinite(latest) and np.isfinite(prev):
            mom_map[str(code)] = latest - prev
    out["quality_mom_roe_pct"] = out["code"].map(
        lambda c: mom_map.get(normalize_stock_code(str(c)))
    )
    return out
