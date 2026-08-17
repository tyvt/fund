# -*- coding: utf-8 -*-
"""总市值截面（stockdb/DuckDB 口径，单位：元）。"""

from __future__ import annotations

import pandas as pd

from dividend_lowvol_rotation.config import MV_TIER_LARGE_CNY, MV_TIER_CAP_ENABLED
from dividend_lowvol_rotation.prices import _batch_load_klines_from_duckdb
from dividend_lowvol_rotation.symbols import normalize_stock_code
from duckdb_market import batch_load_stock_klines, duckdb_available


def market_fields_needed() -> bool:
    return MV_TIER_CAP_ENABLED


def is_small_cap(total_mv: float | None, *, large_threshold_cny: float = MV_TIER_LARGE_CNY) -> bool:
    if total_mv is None or pd.isna(total_mv):
        return False
    return float(total_mv) < large_threshold_cny


def _field_at_series(
    field_df: pd.DataFrame | None,
    as_of: pd.Timestamp,
    field: str,
    *,
    lookback_days: int | None = None,
) -> float | None:
    if field_df is None or field_df.empty or field not in field_df.columns:
        return None
    sub = field_df.copy()
    if "date" in sub.columns:
        sub["date"] = pd.to_datetime(sub["date"])
        sub = sub.sort_values("date")
        sub = sub[sub["date"] <= as_of]
    else:
        sub = sub.sort_index()
        sub = sub[sub.index <= as_of]
    if sub.empty:
        return None
    if lookback_days is not None and lookback_days > 0:
        sub = sub.tail(lookback_days)
    val = pd.to_numeric(sub[field], errors="coerce").dropna()
    if val.empty:
        return None
    if lookback_days is not None and lookback_days > 0:
        return float(val.mean())
    return float(val.iloc[-1])


def total_mv_at_series(mv_df: pd.DataFrame | None, as_of: pd.Timestamp) -> float | None:
    return _field_at_series(mv_df, as_of, "total_mv")


def avg_amount_at_series(
    amt_df: pd.DataFrame | None,
    as_of: pd.Timestamp,
    *,
    lookback_days: int = 20,
) -> float | None:
    return _field_at_series(amt_df, as_of, "amount", lookback_days=lookback_days)


def _batch_fetch_market_fields_from_stockdb(
    codes: list[str],
    start: str,
    end: str,
    *,
    fields: tuple[str, ...] = ("total_mv",),
) -> dict[str, pd.DataFrame]:
    try:
        from dividend_lowvol_rotation.prices import _get_stockdb_client

        client = _get_stockdb_client()
        start_fmt = start.replace("-", "")
        end_fmt = end.replace("-", "")
        field_list = ["date", "code", *fields]
        raw = client.get_data(
            codes,
            start=start_fmt,
            end=end_fmt,
            frequency="1d",
            fields=",".join(field_list),
            as_df=True,
        )
        if raw is None or raw.empty:
            return {}
        if "date" in raw.columns:
            raw["date"] = pd.to_datetime(raw["date"].astype(str), format="%Y%m%d", errors="coerce")
        out: dict[str, pd.DataFrame] = {}
        cols = ["date", *[f for f in fields if f in raw.columns]]
        for code, group in raw.groupby("code"):
            df = group[cols].copy()
            df = df.sort_values("date").reset_index(drop=True)
            out[normalize_stock_code(str(code))] = df
        return out
    except Exception:
        return {}


def batch_load_market_fields(
    codes: list[str],
    start: str,
    end: str,
    *,
    fields: tuple[str, ...] = ("total_mv",),
) -> dict[str, pd.DataFrame]:
    """批量加载总市值序列（元，不复权口径）。"""
    if not codes:
        return {}
    unique = list(dict.fromkeys(normalize_stock_code(c) for c in codes))
    if duckdb_available():
        loaded = _batch_load_klines_from_duckdb(unique, start, end, fields=fields, fq="bfq")
        if loaded:
            return loaded
        loaded = batch_load_stock_klines(unique, start, end, fields=fields, fq="bfq")
        if loaded:
            return loaded
    return _batch_fetch_market_fields_from_stockdb(unique, start, end, fields=fields)


def batch_load_total_mv(codes: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    return batch_load_market_fields(codes, start, end, fields=("total_mv",))


def attach_market_fields(
    df: pd.DataFrame,
    *,
    as_of: pd.Timestamp | None = None,
    store=None,
) -> pd.DataFrame:
    out = df.copy()
    if out.empty or "code" not in out.columns or not market_fields_needed():
        return out
    codes = out["code"].astype(str).tolist()
    as_of = as_of or pd.Timestamp.now().normalize()

    if store is not None and hasattr(store, "total_mv_at"):
        out["total_mv"] = [store.total_mv_at(c, as_of) for c in codes]
        return out

    start = (as_of - pd.Timedelta(days=30)).date().isoformat()
    end = as_of.date().isoformat()
    field_dict = batch_load_market_fields(codes, start, end, fields=("total_mv",))
    mv_vals: list[float | None] = []
    for code in codes:
        nc = normalize_stock_code(code)
        fdf = field_dict.get(nc)
        if fdf is None:
            fdf = field_dict.get(code)
        mv_vals.append(total_mv_at_series(fdf, as_of))
    out["total_mv"] = mv_vals
    return out


def attach_total_mv(
    df: pd.DataFrame,
    *,
    as_of: pd.Timestamp | None = None,
    store=None,
) -> pd.DataFrame:
    return attach_market_fields(df, as_of=as_of, store=store)
