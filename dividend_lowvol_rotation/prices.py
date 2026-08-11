"""Baostock 日 K 线：波动率与价格区间。"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from data_cache import is_fresh_today, load_dataframe, merge_dataframes_by_date, save_dataframe
from dividend_lowvol_rotation.config import (
    BAOSTOCK_BATCH_SLEEP_SEC,
    CACHE_DIR,
    PRICE_HISTORY_BUFFER_DAYS,
    VOL_LOOKBACK_DAYS,
    VOL_TRADING_DAYS_PER_YEAR,
)
from dividend_lowvol_rotation.symbols import normalize_stock_code, to_baostock_code


@contextmanager
def baostock_session():
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
    try:
        yield bs
    finally:
        bs.logout()


def _kline_cache_path(code: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"kline_{normalize_stock_code(code)}.csv"


def _fetch_kline_with_bs(bs, code: str, start: str, end: str) -> pd.DataFrame:
    bs_code = to_baostock_code(code)
    if bs_code is None:
        return pd.DataFrame()
    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,close",
        start_date=start,
        end_date=end,
        frequency="d",
        adjustflag="2",
    )
    rows = []
    while rs.error_code == "0" and rs.next():
        d, c = rs.get_row_data()
        if c and c != "":
            rows.append({"date": d, "close": float(c)})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def _cache_covers(df: pd.DataFrame | None, start: str, end: str, slack_days: int = 7) -> bool:
    if df is None or df.empty:
        return False
    s = pd.Timestamp(start) - pd.Timedelta(days=slack_days)
    e = pd.Timestamp(end) + pd.Timedelta(days=slack_days)
    return df["date"].min() <= s and df["date"].max() >= e


def load_kline_history(
    code: str,
    start: str,
    end: str,
    *,
    bs=None,
    refresh: bool = False,
) -> pd.DataFrame | None:
    """回测用：按区间加载 K 线，优先用本地缓存（不要求当日新鲜）。"""
    path = _kline_cache_path(code)
    cached = None if refresh else load_dataframe(path, parse_dates=["date"])
    if cached is not None and _cache_covers(cached, start, end):
        mask = (cached["date"] >= pd.Timestamp(start)) & (cached["date"] <= pd.Timestamp(end))
        return cached.loc[mask].reset_index(drop=True)

    fetch_start = start
    if cached is not None and not cached.empty:
        fetch_start = min(pd.Timestamp(start), cached["date"].min()).date().isoformat()

    if bs is not None:
        fresh = _fetch_kline_with_bs(bs, code, fetch_start, end)
    else:
        with baostock_session() as session:
            fresh = _fetch_kline_with_bs(session, code, fetch_start, end)

    if fresh is None or fresh.empty:
        if cached is not None and not cached.empty:
            mask = (cached["date"] >= pd.Timestamp(start)) & (cached["date"] <= pd.Timestamp(end))
            return cached.loc[mask].reset_index(drop=True)
        return None

    merged = merge_dataframes_by_date(cached, fresh)
    if merged is not None and not merged.empty:
        save_dataframe(path, merged)
        mask = (merged["date"] >= pd.Timestamp(start)) & (merged["date"] <= pd.Timestamp(end))
        return merged.loc[mask].reset_index(drop=True)
    return fresh


def load_recent_closes(
    code: str,
    refresh: bool = False,
    *,
    bs=None,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame | None:
    path = _kline_cache_path(code)
    end = end or date.today().isoformat()
    start = start or (date.today() - timedelta(days=PRICE_HISTORY_BUFFER_DAYS)).isoformat()
    if not refresh and is_fresh_today(path):
        cached = load_dataframe(path, parse_dates=["date"])
        if cached is not None and len(cached) >= min(30, VOL_LOOKBACK_DAYS // 2):
            return cached
    if bs is not None:
        df = _fetch_kline_with_bs(bs, code, start, end)
    else:
        with baostock_session() as session:
            df = _fetch_kline_with_bs(session, code, start, end)
    if df is None or df.empty:
        return load_dataframe(path, parse_dates=["date"]) if path.exists() else None
    save_dataframe(path, df)
    return df


def compute_volatility_metrics(closes: pd.Series) -> dict:
    prices = pd.to_numeric(closes, errors="coerce").dropna()
    if len(prices) < max(20, VOL_LOOKBACK_DAYS // 2):
        return {
            "ann_vol_pct": None,
            "low_n": None,
            "high_n": None,
            "trading_days": len(prices),
        }
    window = prices.tail(VOL_LOOKBACK_DAYS)
    log_ret = np.log(window / window.shift(1)).dropna()
    ann_vol = float(log_ret.std() * np.sqrt(VOL_TRADING_DAYS_PER_YEAR) * 100)
    return {
        "ann_vol_pct": ann_vol,
        "low_n": float(window.min()),
        "high_n": float(window.max()),
        "trading_days": len(window),
    }


def metrics_as_of(kline: pd.DataFrame, as_of: pd.Timestamp) -> dict:
    """截至 as_of 的收盘价与波动率指标。"""
    if kline is None or kline.empty:
        return {"price": None, "ann_vol_pct": None, "low_n": None, "high_n": None}
    sub = kline[kline["date"] <= as_of].copy()
    if sub.empty:
        return {"price": None, "ann_vol_pct": None, "low_n": None, "high_n": None}
    price = float(sub["close"].iloc[-1])
    vol = compute_volatility_metrics(sub["close"])
    return {"price": price, **vol}


def batch_load_volatility(
    codes: list[str],
    refresh: bool = False,
    sleep_sec: float = BAOSTOCK_BATCH_SLEEP_SEC,
) -> pd.DataFrame:
    rows = []
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=PRICE_HISTORY_BUFFER_DAYS)).isoformat()
    with baostock_session() as bs:
        for i, code in enumerate(codes):
            kline = load_recent_closes(code, refresh=refresh, bs=bs, start=start, end=end)
            if kline is None or kline.empty:
                continue
            metrics = compute_volatility_metrics(kline["close"])
            if metrics["ann_vol_pct"] is None:
                continue
            rows.append({"code": normalize_stock_code(code), **metrics})
            if sleep_sec > 0 and i + 1 < len(codes):
                time.sleep(sleep_sec)
    return pd.DataFrame(rows)
