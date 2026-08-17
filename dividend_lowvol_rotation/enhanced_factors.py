# -*- coding: utf-8 -*-
"""Beta 计算（行业分散约束用）。"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

from dividend_lowvol_rotation.config import BETA_BENCHMARK_CODE, BETA_LOOKBACK_DAYS
from dividend_lowvol_rotation.symbols import normalize_stock_code


def _benchmark_daily_returns_from_kline(
    kline: pd.DataFrame | None,
    end: pd.Timestamp,
    lookback: int,
) -> pd.Series | None:
    if kline is None or kline.empty:
        return None
    k = kline.sort_values("date")
    k = k[k["date"] <= end]
    if len(k) < 60:
        return None
    ret = k.set_index("date")["close"].astype(float).pct_change().dropna()
    return ret.tail(lookback + 30)


@lru_cache(maxsize=64)
def _benchmark_daily_returns(
    benchmark: str,
    end: str,
    lookback: int,
) -> pd.Series | None:
    from dividend_lowvol_rotation.prices import load_kline_history

    end_ts = pd.Timestamp(end)
    start = (end_ts - pd.Timedelta(days=lookback * 2 + 60)).date().isoformat()
    k = load_kline_history(benchmark, start, end)
    return _benchmark_daily_returns_from_kline(k, end_ts, lookback)


def compute_beta_from_kline(
    kline: pd.DataFrame,
    as_of: pd.Timestamp,
    bench_ret: pd.Series | None,
    lookback: int = BETA_LOOKBACK_DAYS,
) -> float | None:
    if kline is None or kline.empty or bench_ret is None or bench_ret.empty:
        return None
    sub = kline[kline["date"] <= as_of].tail(lookback + 30)
    if len(sub) < 60:
        return None
    stock_ret = sub.set_index("date")["close"].astype(float).pct_change()
    aligned = pd.concat([stock_ret, bench_ret], axis=1, join="inner").dropna()
    aligned.columns = ["s", "b"]
    aligned = aligned.tail(lookback)
    if len(aligned) < 40:
        return None
    cov = np.cov(aligned["s"], aligned["b"])
    if cov[1, 1] <= 0:
        return None
    return float(cov[0, 1] / cov[1, 1])


def attach_enhanced_factors(
    panel: pd.DataFrame,
    *,
    records: pd.DataFrame | None = None,
    risk_hist: pd.DataFrame | None = None,
    as_of: pd.Timestamp,
    store=None,
    bond_yield_pct: float | None = None,
    allow_network: bool = True,
) -> pd.DataFrame:
    del records, risk_hist, bond_yield_pct, allow_network
    if panel.empty:
        return panel
    out = panel.copy()
    codes = out["code"].map(normalize_stock_code).tolist()
    betas: list[float | None] = [None] * len(out)

    bench_ret = None
    if store is not None:
        bench_kline = store.kline_df(BETA_BENCHMARK_CODE)
        bench_ret = _benchmark_daily_returns_from_kline(bench_kline, as_of, BETA_LOOKBACK_DAYS)
        if bench_ret is None:
            bench_ret = _benchmark_daily_returns(
                BETA_BENCHMARK_CODE, as_of.date().isoformat(), BETA_LOOKBACK_DAYS
            )

    if store is not None:
        for i, code in enumerate(codes):
            betas[i] = compute_beta_from_kline(store.kline_df(code), as_of, bench_ret)

    out["beta_252"] = betas
    return out
