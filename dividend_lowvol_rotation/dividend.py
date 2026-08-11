"""分红数据：fhps 批次、TTM 累计与动态股息率分子。"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from data_cache import is_fresh_today, load_dataframe, save_dataframe
from dividend_lowvol_rotation.config import (
    CACHE_DIR,
    DIVIDEND_YIELD_MODE,
    LATEST_DIVIDEND_STALE_DAYS,
    TTM_LOOKBACK_DAYS,
    resolve_fhps_report_dates,
)
from dividend_lowvol_rotation.symbols import normalize_stock_code


def _fetch_fhps_batch(report_date: str) -> pd.DataFrame:
    import akshare as ak

    df = ak.stock_fhps_em(date=report_date)
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["report_date"] = report_date
    out["code"] = out["代码"].map(normalize_stock_code)
    out["name"] = out["名称"].astype(str)
    out["ex_date"] = pd.to_datetime(out["除权除息日"], errors="coerce")
    out["cash_per_10"] = pd.to_numeric(out["现金分红-现金分红比例"], errors="coerce")
    out["fhps_yield_pct"] = pd.to_numeric(out["现金分红-股息率"], errors="coerce") * 100
    out["progress"] = out["方案进度"].astype(str)
    out["eps"] = pd.to_numeric(out["每股收益"], errors="coerce")
    out["bps"] = pd.to_numeric(out["每股净资产"], errors="coerce")
    out["profit_yoy_pct"] = pd.to_numeric(out["净利润同比增长"], errors="coerce")
    return out[
        [
            "code",
            "name",
            "ex_date",
            "cash_per_10",
            "fhps_yield_pct",
            "progress",
            "report_date",
            "eps",
            "bps",
            "profit_yoy_pct",
        ]
    ]


def load_fhps_all_records(
    refresh: bool = False,
    *,
    backtest_start: str | None = None,
) -> pd.DataFrame:
    """全部已实施分红记录（含历史多次派息）。"""
    path = CACHE_DIR / "fhps_all_records.csv"
    report_dates = resolve_fhps_report_dates(backtest_start)
    frames: list[pd.DataFrame] = []
    rebuilt = False

    for report_date in report_dates:
        batch_path = CACHE_DIR / f"fhps_{report_date}.csv"
        part = None
        if not refresh and batch_path.exists() and is_fresh_today(batch_path):
            part = load_dataframe(batch_path, parse_dates=["ex_date"])
            if part is not None and not part.empty:
                part["code"] = part["code"].map(normalize_stock_code)
        else:
            try:
                part = _fetch_fhps_batch(report_date)
                if part is not None and not part.empty:
                    save_dataframe(batch_path, part)
                    rebuilt = True
            except Exception:
                part = load_dataframe(batch_path, parse_dates=["ex_date"])
                if part is not None and not part.empty:
                    part["code"] = part["code"].map(normalize_stock_code)
        if part is not None and not part.empty:
            frames.append(part)

    if not frames:
        if not refresh and is_fresh_today(path):
            cached = load_dataframe(path, parse_dates=["ex_date"])
            if cached is not None and not cached.empty:
                cached["code"] = cached["code"].map(normalize_stock_code)
                return cached
        return pd.DataFrame()

    merged = pd.concat(frames, ignore_index=True)
    merged = merged[merged["progress"].str.contains("实施", na=False)]
    merged = merged.dropna(subset=["code", "ex_date", "cash_per_10"])
    merged = merged[merged["cash_per_10"] > 0]
    merged["cash_per_share"] = merged["cash_per_10"] / 10.0
    merged["code"] = merged["code"].map(normalize_stock_code)
    merged = merged.drop_duplicates(subset=["code", "ex_date", "cash_per_share"], keep="last")

    if rebuilt or refresh or not is_fresh_today(path):
        save_dataframe(path, merged)
    return merged.reset_index(drop=True)


def _latest_per_stock(records: pd.DataFrame, as_of: pd.Timestamp | None = None) -> pd.DataFrame:
    today = as_of or pd.Timestamp(date.today())
    sub = records[records["ex_date"] <= today].copy()
    if sub.empty:
        return sub
    sub = sub.sort_values(["code", "ex_date", "report_date"])
    latest = sub.groupby("code", as_index=False).tail(1)
    return latest.reset_index(drop=True)


def _ttm_per_stock(records: pd.DataFrame, as_of: pd.Timestamp | None = None) -> pd.DataFrame:
    today = as_of or pd.Timestamp(date.today())
    start = today - timedelta(days=TTM_LOOKBACK_DAYS)
    sub = records[(records["ex_date"] > start) & (records["ex_date"] <= today)].copy()
    if sub.empty:
        return pd.DataFrame()
    agg = (
        sub.groupby("code", as_index=False)
        .agg(
            cash_per_share=("cash_per_share", "sum"),
            ex_date=("ex_date", "max"),
            name=("name", "last"),
            eps=("eps", "last"),
            bps=("bps", "last"),
            profit_yoy_pct=("profit_yoy_pct", "last"),
            ttm_div_count=("ex_date", "count"),
        )
    )
    agg["dividend_mode"] = "ttm"
    return agg


def filter_records_as_of(records: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """回测时点：仅使用 as_of 当日已公告的分红批次。"""
    if records is None or records.empty or "report_date" not in records.columns:
        return records
    cutoff = as_of.strftime("%Y%m%d")
    return records[records["report_date"].astype(str) <= cutoff].copy()


def build_dividend_panel(
    records: pd.DataFrame | None = None,
    refresh: bool = False,
    as_of: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """按配置合并 latest / ttm / auto 股息率分子。"""
    if records is None:
        records = load_fhps_all_records(refresh=refresh)
    if records.empty:
        return pd.DataFrame()

    today = as_of or pd.Timestamp(date.today())
    records = filter_records_as_of(records, today)
    if records.empty:
        return pd.DataFrame()
    latest = _latest_per_stock(records, today)
    latest = latest.copy()
    latest["dividend_mode"] = "latest"

    mode = DIVIDEND_YIELD_MODE
    if mode == "latest":
        out = latest
    elif mode == "ttm":
        ttm = _ttm_per_stock(records, today)
        out = ttm if not ttm.empty else latest
    else:
        # auto：最新派息距今过久则用 TTM
        ttm = _ttm_per_stock(records, today)
        stale_days = (today - pd.to_datetime(latest["ex_date"])).dt.days
        use_ttm = stale_days > LATEST_DIVIDEND_STALE_DAYS
        if not ttm.empty:
            ttm_codes = set(ttm["code"])
            latest_stale = latest[use_ttm & latest["code"].isin(ttm_codes)].copy()
            latest_fresh = latest[~use_ttm | ~latest["code"].isin(ttm_codes)].copy()
            parts = [latest_fresh]
            if not latest_stale.empty:
                parts.append(ttm[ttm["code"].isin(latest_stale["code"])])
            out = pd.concat(parts, ignore_index=True)
            out.loc[out["code"].isin(latest_stale["code"]), "dividend_mode"] = "ttm_auto"
        else:
            out = latest

    out["code"] = out["code"].map(normalize_stock_code)
    return out.reset_index(drop=True)


def load_fhps_merged(refresh: bool = False) -> pd.DataFrame:
    """兼容旧接口：返回当前股息面板。"""
    return build_dividend_panel(refresh=refresh)
