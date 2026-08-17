# -*- coding: utf-8 -*-
"""质量动量（ROE 同比变化）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from dividend_lowvol_rotation.symbols import normalize_stock_code


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
