"""日 K 线：优先 DuckDB，回退 CSV 缓存与 StockDB。"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from data_cache import is_fresh_today, load_dataframe, merge_dataframes_by_date, save_dataframe
from duckdb_market import batch_load_stock_klines, duckdb_available, load_index_kline, load_stock_kline
from dividend_lowvol_rotation.config import (
    CACHE_DIR,
    MOMENTUM_MA_DAYS,
    MOMENTUM_RETURN_DAYS,
    MOMENTUM_SELL_MA_DAYS,
    PRICE_HISTORY_BUFFER_DAYS,
    STOP_ATR_LOOKBACK,
    VOL_LOOKBACK_DAYS,
    VOL_TRADING_DAYS_PER_YEAR,
)
from dividend_lowvol_rotation.symbols import normalize_stock_code


# stockdb SDK 路径
STOCKDB_SDK_PATH = r"D:\repository\stockdb\pybao"

def _get_stockdb_client():
    """获取 stockdb 客户端"""
    if STOCKDB_SDK_PATH not in sys.path:
        sys.path.insert(0, STOCKDB_SDK_PATH)
    from stock_sdk import StockDBClient
    return StockDBClient(host="127.0.0.1", port=7899)


def _kline_cache_path(code: str, *, fq: str | None = "qfq") -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "" if fq == "qfq" else "_bfq"
    return CACHE_DIR / f"kline_{normalize_stock_code(code)}{suffix}.csv"


def _cache_covers(df: pd.DataFrame | None, start: str, end: str, slack_days: int = 7) -> bool:
    if df is None or df.empty:
        return False
    s = pd.Timestamp(start) - pd.Timedelta(days=slack_days)
    today = pd.Timestamp.now().normalize()
    end_ts = pd.Timestamp(end)
    if end_ts >= today:
        effective_end = today - pd.Timedelta(days=1)
    else:
        effective_end = end_ts + pd.Timedelta(days=slack_days)
    return df["date"].min() <= s and df["date"].max() >= effective_end


def _load_kline_from_duckdb(
    code: str, start: str, end: str, *, fields: tuple[str, ...] = ("close",)
) -> pd.DataFrame | None:
    if not duckdb_available():
        return None
    df = load_stock_kline(code, start, end, fields=fields)
    if df is not None and not df.empty:
        return df
    return load_index_kline(code, start, end, fields=fields)


def _batch_load_klines_from_duckdb(
    codes: list[str],
    start: str,
    end: str,
    *,
    fields: tuple[str, ...] = ("close",),
    fq: str | None = None,
) -> dict[str, pd.DataFrame]:
    if not duckdb_available():
        return {}
    return batch_load_stock_klines(codes, start, end, fields=fields, fq=fq)


def _fetch_kline_from_stockdb(
    code: str, start: str, end: str, *, fq: str | None = "qfq"
) -> pd.DataFrame:
    """使用 stockdb 批量查询接口获取单只股票 K 线数据"""
    try:
        client = _get_stockdb_client()
        # stockdb 日期格式为 YYYYMMDD
        start_fmt = start.replace("-", "")
        end_fmt = end.replace("-", "")
        
        k = client.get_data(
            code,
            start=start_fmt,
            end=end_fmt,
            frequency="1d",
            fields="date,close",
            fq="qfq" if fq == "qfq" else "bfq",
            as_df=True
        )
        
        if k is None or k.empty:
            return pd.DataFrame()
        
        # stockdb 返回的 date 是整数格式 20250110，需要转换
        if "date" in k.columns:
            k["date"] = pd.to_datetime(k["date"].astype(str), format="%Y%m%d")
        
        # 只保留 date 和 close 列
        if "close" in k.columns:
            k = k[["date", "close"]].copy()
            k = k.sort_values("date").reset_index(drop=True)
            return k
        
        return pd.DataFrame()
    except Exception as e:
        print(f"stockdb 查询 {code} 失败: {e}")
        return pd.DataFrame()


def _batch_fetch_klines_from_stockdb(
    codes: list[str],
    start: str,
    end: str,
    *,
    fq: str | None = "qfq",
) -> dict[str, pd.DataFrame]:
    """使用 stockdb 批量查询接口一次性获取所有股票 K 线"""
    try:
        client = _get_stockdb_client()
        start_fmt = start.replace("-", "")
        end_fmt = end.replace("-", "")
        
        # 批量查询所有股票
        k = client.get_data(
            codes,
            start=start_fmt,
            end=end_fmt,
            frequency="1d",
            fields="date,code,close",
            fq="qfq" if fq == "qfq" else "bfq",
            as_df=True
        )
        
        if k is None or k.empty:
            return {}
        
        # 转换日期格式
        if "date" in k.columns:
            k["date"] = pd.to_datetime(k["date"].astype(str), format="%Y%m%d")
        
        result = {}
        # 按股票代码分组
        if "code" in k.columns:
            for code, group in k.groupby("code"):
                df = group[["date", "close"]].copy()
                df = df.sort_values("date").reset_index(drop=True)
                result[code] = df
        
        return result
    except Exception as e:
        print(f"stockdb 批量查询失败: {e}")
        return {}


def load_kline_history(
    code: str,
    start: str,
    end: str,
    *,
    refresh: bool = False,
) -> pd.DataFrame | None:
    """回测用：按区间加载 K 线，优先 DuckDB / 本地缓存。"""
    path = _kline_cache_path(code, fq="qfq")
    cached = None if refresh else load_dataframe(path, parse_dates=["date"])
    if cached is not None and _cache_covers(cached, start, end):
        mask = (cached["date"] >= pd.Timestamp(start)) & (cached["date"] <= pd.Timestamp(end))
        return cached.loc[mask].reset_index(drop=True)

    duck = None if refresh else _load_kline_from_duckdb(code, start, end)
    if duck is not None and not duck.empty:
        if "close" in duck.columns:
            out = duck[["date", "close"]].copy()
        else:
            out = duck.copy()
        merged = merge_dataframes_by_date(cached, out)
        if merged is not None and not merged.empty:
            save_dataframe(path, merged)
        mask = (out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))
        return out.loc[mask].reset_index(drop=True)

    fetch_start = start
    if cached is not None and not cached.empty:
        fetch_start = min(pd.Timestamp(start), cached["date"].min()).date().isoformat()

    fresh = _fetch_kline_from_stockdb(code, fetch_start, end)

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
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame | None:
    path = _kline_cache_path(code, fq="qfq")
    end = end or date.today().isoformat()
    start = start or (date.today() - timedelta(days=PRICE_HISTORY_BUFFER_DAYS)).isoformat()
    if not refresh and is_fresh_today(path):
        cached = load_dataframe(path, parse_dates=["date"])
        if cached is not None and len(cached) >= min(30, VOL_LOOKBACK_DAYS // 2):
            return cached

    duck = _load_kline_from_duckdb(code, start, end)
    if duck is not None and not duck.empty and "close" in duck.columns:
        df = duck[["date", "close"]].copy()
        save_dataframe(path, df)
        return df

    df = _fetch_kline_from_stockdb(code, start, end)
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


def precompute_kline_metrics(kline: pd.DataFrame) -> pd.DataFrame:
    """一次性向量化计算全历史滚动波动率，供回测按日快速查询。"""
    if kline is None or kline.empty:
        return pd.DataFrame()
    df = kline.sort_values("date").copy()
    closes = pd.to_numeric(df["close"], errors="coerce")
    min_periods = max(20, VOL_LOOKBACK_DAYS // 2)
    log_ret = np.log(closes / closes.shift(1))
    ann_vol = (
        log_ret.rolling(VOL_LOOKBACK_DAYS, min_periods=min_periods).std()
        * np.sqrt(VOL_TRADING_DAYS_PER_YEAR)
        * 100
    ).clip(upper=150.0)
    low_n = closes.rolling(VOL_LOOKBACK_DAYS, min_periods=min_periods).min()
    high_n = closes.rolling(VOL_LOOKBACK_DAYS, min_periods=min_periods).max()
    ma_200 = closes.rolling(MOMENTUM_SELL_MA_DAYS, min_periods=max(60, MOMENTUM_SELL_MA_DAYS // 2)).mean()
    ma_250 = closes.rolling(MOMENTUM_MA_DAYS, min_periods=max(60, MOMENTUM_MA_DAYS // 2)).mean()
    ret_12m = closes / closes.shift(MOMENTUM_RETURN_DAYS) - 1.0
    tr = closes.diff().abs()
    atr = tr.rolling(STOP_ATR_LOOKBACK, min_periods=max(5, STOP_ATR_LOOKBACK // 2)).mean()
    out = pd.DataFrame(
        {
            "price": closes.values,
            "ann_vol_pct": ann_vol.values,
            "low_n": low_n.values,
            "high_n": high_n.values,
            "ma_200": ma_200.values,
            "ma_250": ma_250.values,
            "ret_12m": ret_12m.values,
            "atr": atr.values,
        },
        index=pd.DatetimeIndex(df["date"].values),
    )
    return out


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


def metrics_from_precomputed(metrics_df: pd.DataFrame, as_of: pd.Timestamp) -> dict:
    """从 precompute_kline_metrics 结果按日查询。"""
    empty = {
        "price": None,
        "ann_vol_pct": None,
        "low_n": None,
        "high_n": None,
        "ma_200": None,
        "ma_250": None,
        "ret_12m": None,
        "atr": None,
    }
    if metrics_df is None or metrics_df.empty:
        return empty
    sub = metrics_df[metrics_df.index <= as_of]
    if sub.empty:
        return empty
    row = sub.iloc[-1]
    price = row["price"]
    if pd.isna(price):
        return empty
    out = {
        "price": float(price),
        "ann_vol_pct": float(row["ann_vol_pct"]) if pd.notna(row["ann_vol_pct"]) else None,
        "low_n": float(row["low_n"]) if pd.notna(row["low_n"]) else None,
        "high_n": float(row["high_n"]) if pd.notna(row["high_n"]) else None,
    }
    for col in ("ma_200", "ma_250", "ret_12m", "atr"):
        if col in row.index and pd.notna(row[col]):
            out[col] = float(row[col])
        else:
            out[col] = None
    return out


def batch_load_volatility(
    codes: list[str],
    refresh: bool = False,
) -> pd.DataFrame:
    """批量加载波动率指标，优先 DuckDB。"""
    rows = []
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=PRICE_HISTORY_BUFFER_DAYS)).isoformat()

    kline_dict = _batch_load_klines_from_duckdb(codes, start, end)
    missing = [c for c in codes if c not in kline_dict or kline_dict[c].empty]
    if missing:
        stockdb_dict = _batch_fetch_klines_from_stockdb(missing, start, end)
        kline_dict.update(stockdb_dict)

    for code in codes:
        nc = normalize_stock_code(code)
        kline = kline_dict.get(nc)
        if kline is None or kline.empty:
            kline = kline_dict.get(code)
        if kline is None or kline.empty:
            continue
        close_col = "close" if "close" in kline.columns else kline.columns[-1]
        metrics = compute_volatility_metrics(kline[close_col])
        if metrics["ann_vol_pct"] is None:
            continue
        rows.append({"code": normalize_stock_code(code), **metrics})
    
    return pd.DataFrame(rows)
