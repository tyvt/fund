# -*- coding: utf-8 -*-
"""除权除息公司行为：送股/转增（与 RQAlpha bundle get_split 对齐）。"""

from __future__ import annotations

import pandas as pd


def build_split_index(records: pd.DataFrame | None) -> dict[str, pd.DataFrame]:
    """按股票代码索引送股记录（按除权日排序）。"""
    if records is None or records.empty:
        return {}
    sub = records.dropna(subset=["code", "ex_date", "factor"]).copy()
    sub["ex_date"] = pd.to_datetime(sub["ex_date"]).dt.normalize()
    sub = sub[sub["factor"] > 1.0 + 1e-9].sort_values("ex_date")
    return {str(code): grp.reset_index(drop=True) for code, grp in sub.groupby("code")}


def adjust_holdings_for_splits(
    holdings: dict[str, int],
    split_index: dict[str, pd.DataFrame],
    as_of: pd.Timestamp,
    buy_dates: dict[str, pd.Timestamp] | None = None,
    *,
    include_ex_date: bool = True,
) -> dict[str, int]:
    """补齐 RQ 未自动调整的送股后股数（与原生 backtest 逐日 apply_splits 对齐）。

    include_ex_date=False 时仅计入 as_of 之前除权（除权日当日派息仍按除权前股数）。
    """
    if not holdings:
        return {}
    if not split_index:
        return {str(c): int(s) for c, s in holdings.items() if int(s) > 0}
    day_end = pd.Timestamp(as_of).normalize()
    bought_on = {
        str(code): pd.Timestamp(dt).normalize()
        for code, dt in (buy_dates or {}).items()
        if dt is not None
    }
    out: dict[str, int] = {}
    for code, shares in holdings.items():
        sh = int(shares)
        if sh <= 0:
            continue
        code = str(code)
        splits = split_index.get(code)
        if splits is None or splits.empty:
            out[code] = sh
            continue
        bought = bought_on.get(code)
        for _, sp in splits.sort_values("ex_date").iterrows():
            ex_day = pd.Timestamp(sp["ex_date"]).normalize()
            if ex_day > day_end:
                continue
            if not include_ex_date and ex_day >= day_end:
                continue
            if bought is not None and ex_day <= bought:
                continue
            factor = float(sp["factor"])
            if factor > 1.0:
                sh = int(sh * factor)
        out[code] = sh
    return out


def apply_splits_to_holdings(
    holdings: dict[str, int],
    split_index: dict[str, pd.DataFrame],
    as_of: pd.Timestamp,
) -> list[dict]:
    """除权日按 factor 更新持仓股数 dict（与 apply_splits_on_date 一致）。"""
    if not holdings or not split_index:
        return []
    day = pd.Timestamp(as_of).normalize()
    rows: list[dict] = []
    for code in list(holdings.keys()):
        splits = split_index.get(str(code))
        if splits is None or splits.empty:
            continue
        mask = splits["ex_date"] == day
        if not mask.any():
            continue
        for _, sp in splits.loc[mask].iterrows():
            factor = float(sp["factor"])
            if factor <= 1.0:
                continue
            old_shares = int(holdings[code])
            new_shares = int(old_shares * factor)
            if new_shares <= old_shares:
                continue
            holdings[code] = new_shares
            rows.append(
                {
                    "ex_date": day.date().isoformat(),
                    "code": str(code),
                    "factor": round(factor, 6),
                    "shares_before": old_shares,
                    "shares_after": new_shares,
                }
            )
    return rows


def apply_splits_on_date(
    lots: dict,
    split_index: dict[str, pd.DataFrame],
    as_of: pd.Timestamp,
) -> list[dict]:
    """在除权日按 factor 增加持仓股数（成本总额不变，调低每股成本）。"""
    if not lots or not split_index:
        return []
    day = pd.Timestamp(as_of).normalize()
    rows: list[dict] = []
    for code, lot in lots.items():
        splits = split_index.get(str(code))
        if splits is None or splits.empty:
            continue
        mask = splits["ex_date"] == day
        if not mask.any():
            continue
        for _, sp in splits.loc[mask].iterrows():
            factor = float(sp["factor"])
            if factor <= 1.0:
                continue
            old_shares = int(lot.shares)
            new_shares = int(old_shares * factor)
            if new_shares <= old_shares:
                continue
            lot.shares = new_shares
            lot.buy_price = float(lot.buy_price) / factor
            if getattr(lot, "peak_price", 0) > 0:
                lot.peak_price = float(lot.peak_price) / factor
            if getattr(lot, "prev_price", 0) > 0:
                lot.prev_price = float(lot.prev_price) / factor
            rows.append(
                {
                    "ex_date": day.date().isoformat(),
                    "code": str(code),
                    "name": getattr(lot, "name", ""),
                    "factor": round(factor, 6),
                    "shares_before": old_shares,
                    "shares_after": new_shares,
                }
            )
    return rows
