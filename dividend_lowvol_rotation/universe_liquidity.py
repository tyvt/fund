# -*- coding: utf-8 -*-
"""全市场流动性截面：对标 H30269 市值/成交额排名前 90% 保留规则。"""

from __future__ import annotations

import sys
from datetime import date

from pathlib import Path

import numpy as np
import pandas as pd

from config import STOCKDB_HOST, STOCKDB_PORT, STOCKDB_SDK_PATH
from data_cache import load_dataframe, save_dataframe
from duckdb_market import batch_load_stock_klines, duckdb_available, list_a_share_codes
from dividend_lowvol_rotation.config import (
    CACHE_DIR,
    INDEX_RETENTION_LIQUIDITY_ENABLED,
    INDEX_RETENTION_LIQUIDITY_LOOKBACK_DAYS,
    INDEX_RETENTION_LIQUIDITY_PERCENTILE,
)
from dividend_lowvol_rotation.symbols import normalize_stock_code


def _get_stockdb_client():
    if STOCKDB_SDK_PATH and str(STOCKDB_SDK_PATH) not in sys.path:
        sys.path.insert(0, str(STOCKDB_SDK_PATH))
    from stock_sdk import StockDBClient

    return StockDBClient(host=STOCKDB_HOST, port=STOCKDB_PORT)


def list_all_a_share_codes() -> list[str]:
    """全 A 股代码：优先 DuckDB 快照，回退 stockdb。"""
    codes = list_a_share_codes(scope="all")
    if codes:
        return codes
    try:
        from stockdb import init

        rd = init(host=STOCKDB_HOST, port=STOCKDB_PORT)
        payload = rd.get("股票代码")
        if not payload:
            return []
        if not isinstance(payload, dict):
            try:
                payload = dict(payload)
            except (TypeError, ValueError):
                return []
        out: list[str] = []
        for items in payload.values():
            if isinstance(items, list):
                out.extend(normalize_stock_code(str(c)) for c in items)
        return sorted(set(c for c in out if c))
    except Exception:
        return []


def _liquidity_cache_path(as_of: pd.Timestamp) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"universe_liquidity_{as_of.date().isoformat()}.csv"


def _mean_liquidity_from_kline(df: pd.DataFrame) -> dict | None:
    if df is None or df.empty:
        return None
    sub = df.copy()
    if "date" in sub.columns:
        sub["date"] = pd.to_datetime(sub["date"])
        sub = sub.sort_values("date")
    mv = (
        pd.to_numeric(sub["total_mv"], errors="coerce").dropna()
        if "total_mv" in sub.columns
        else pd.Series(dtype=float)
    )
    amt = (
        pd.to_numeric(sub["amount"], errors="coerce").dropna()
        if "amount" in sub.columns
        else pd.Series(dtype=float)
    )
    if mv.empty and amt.empty:
        return None
    return {
        "avg_total_mv": float(mv.mean()) if not mv.empty else np.nan,
        "avg_amount": float(amt.mean()) if not amt.empty else np.nan,
        "obs_days": int(max(len(mv), len(amt))),
    }


def _load_liquidity_from_duckdb(as_of: pd.Timestamp) -> pd.DataFrame | None:
    try:
        from duckdb_store import get_connection

        conn = get_connection(read_only=True)
        try:
            df = conn.execute(
                """
                SELECT code, avg_total_mv, avg_amount, obs_days, as_of
                FROM dlv_liquidity
                WHERE as_of = ?
                """,
                [as_of.date()],
            ).fetchdf()
        finally:
            conn.close()
        if df is None or df.empty:
            return None
        df["code"] = df["code"].map(normalize_stock_code)
        df["as_of"] = as_of.date().isoformat()
        return df
    except Exception:
        return None


def fetch_universe_liquidity_snapshot(
    as_of: pd.Timestamp,
    *,
    lookback_days: int = INDEX_RETENTION_LIQUIDITY_LOOKBACK_DAYS,
    codes: list[str] | None = None,
    chunk_size: int = 200,
    verbose: bool = False,
) -> pd.DataFrame:
    """过去 lookback_days 内日均市值、日均成交额（全 A 股截面）。"""
    as_of = pd.Timestamp(as_of).normalize()
    cache_path = _liquidity_cache_path(as_of)
    cached = load_dataframe(cache_path)
    if cached is not None and not cached.empty and "code" in cached.columns:
        out = cached.copy()
        out["code"] = out["code"].map(normalize_stock_code)
        return out

    duck = _load_liquidity_from_duckdb(as_of)
    if duck is not None and not duck.empty:
        save_dataframe(cache_path, duck)
        return duck

    codes = codes or list_all_a_share_codes()
    if not codes:
        return pd.DataFrame()

    start_iso = (as_of - pd.Timedelta(days=int(lookback_days * 1.6))).date().isoformat()
    end_iso = as_of.date().isoformat()
    rows: list[dict] = []
    total = len(codes)

    if duckdb_available():
        if verbose:
            print(f"  流动性截面：DuckDB 加载 {total} 只…")
        for i in range(0, total, chunk_size):
            chunk = codes[i : i + chunk_size]
            kline_dict = batch_load_stock_klines(
                chunk, start_iso, end_iso, fields=("total_mv", "amount")
            )
            for code, df in kline_dict.items():
                stats = _mean_liquidity_from_kline(df)
                if stats:
                    rows.append({"code": normalize_stock_code(code), **stats})
            if verbose and i % (chunk_size * 5) == 0 and i > 0:
                print(f"  流动性截面 {i}/{total}…")
    else:
        end_fmt = as_of.strftime("%Y%m%d")
        start_fmt = (as_of - pd.Timedelta(days=int(lookback_days * 1.6))).strftime("%Y%m%d")
        client = _get_stockdb_client()
        for i in range(0, total, chunk_size):
            chunk = codes[i : i + chunk_size]
            if verbose and i % (chunk_size * 5) == 0:
                print(f"  流动性截面 {i}/{total}…")
            try:
                k = client.get_data(
                    chunk,
                    start=start_fmt,
                    end=end_fmt,
                    frequency="1d",
                    fields="date,code,total_mv,amount",
                    fq=None,
                    as_df=True,
                )
            except Exception:
                continue

            if isinstance(k, pd.DataFrame):
                if "code" not in k.columns:
                    continue
                for code, grp in k.groupby("code"):
                    stats = _mean_liquidity_from_kline(grp)
                    if stats:
                        rows.append({"code": normalize_stock_code(str(code)), **stats})
            elif isinstance(k, dict):
                for code, records in k.items():
                    if not records:
                        continue
                    df = pd.DataFrame(records)
                    stats = _mean_liquidity_from_kline(df)
                    if stats:
                        rows.append({"code": normalize_stock_code(str(code)), **stats})

    if not rows:
        return pd.DataFrame()

    snap = pd.DataFrame(rows).drop_duplicates(subset=["code"], keep="last")
    snap["as_of"] = as_of.date().isoformat()
    save_dataframe(cache_path, snap)
    return snap


def liquidity_retention_thresholds(
    snap: pd.DataFrame,
    *,
    percentile: float = INDEX_RETENTION_LIQUIDITY_PERCENTILE,
) -> tuple[float | None, float | None]:
    """前 percentile（默认 90%）对应的市值/成交额下限。"""
    if snap is None or snap.empty:
        return None, None
    tail = 1.0 - percentile
    mv = pd.to_numeric(snap.get("avg_total_mv"), errors="coerce").dropna()
    amt = pd.to_numeric(snap.get("avg_amount"), errors="coerce").dropna()
    mv_cut = float(mv.quantile(tail)) if len(mv) >= 20 else None
    amt_cut = float(amt.quantile(tail)) if len(amt) >= 20 else None
    return mv_cut, amt_cut


def liquidity_retention_fail_reason(
    code: str,
    snap: pd.DataFrame,
    *,
    percentile: float = INDEX_RETENTION_LIQUIDITY_PERCENTILE,
) -> str | None:
    if snap is None or snap.empty:
        return None
    code = normalize_stock_code(code)
    sub = snap[snap["code"].astype(str) == code]
    if sub.empty:
        return None
    row = sub.iloc[-1]
    mv_cut, amt_cut = liquidity_retention_thresholds(snap, percentile=percentile)
    avg_mv = pd.to_numeric(row.get("avg_total_mv"), errors="coerce")
    avg_amt = pd.to_numeric(row.get("avg_amount"), errors="coerce")
    if mv_cut is not None and (pd.isna(avg_mv) or float(avg_mv) < mv_cut):
        return f"市值不在全市场前{percentile:.0%}"
    if amt_cut is not None and (pd.isna(avg_amt) or float(avg_amt) < amt_cut):
        return f"成交额不在全市场前{percentile:.0%}"
    return None


def get_liquidity_snapshot(
    as_of: pd.Timestamp,
    *,
    cache: dict[str, pd.DataFrame] | None = None,
    verbose: bool = False,
) -> pd.DataFrame:
    key = pd.Timestamp(as_of).date().isoformat()
    if cache is not None and key in cache:
        return cache[key]
    snap = fetch_universe_liquidity_snapshot(as_of, verbose=verbose)
    if cache is not None:
        cache[key] = snap
    return snap
