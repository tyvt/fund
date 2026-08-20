# -*- coding: utf-8 -*-
"""从 RQAlpha bundle 读取日 K（与回测 bar_dict 同源，默认不复权）。"""

from __future__ import annotations

import os
from datetime import datetime
from functools import lru_cache
from types import SimpleNamespace

import pandas as pd

from dividend_lowvol_rotation.config import RQALPHA_BUNDLE_PATH
from dividend_lowvol_rotation.rqalpha.symbols_rq import to_rqalpha_id
from dividend_lowvol_rotation.symbols import normalize_stock_code


@lru_cache(maxsize=1)
def get_rqalpha_data_source(bundle_path: str | None = None):
    path = bundle_path or os.environ.get("RQALPHA_BUNDLE_PATH") or RQALPHA_BUNDLE_PATH
    if not os.path.isdir(path):
        raise FileNotFoundError(f"RQAlpha bundle 不存在：{path}（请先 rqalpha download-bundle）")
    try:
        from rqalpha.data.base_data_source.data_source import BaseDataSource
    except ImportError as exc:
        raise ImportError(
            "读取 RQAlpha bundle 需要安装 rqalpha。"
            "请运行 scripts\\setup_rqalpha_env.bat，"
            "或使用 run_dividend_lowvol_backtest.bat / "
            "rqalpha_env\\Scripts\\python.exe -m dividend_lowvol_rotation.backtest。"
            "若暂不装 RQAlpha，可设 DLV_BACKTEST_PRICE_SOURCE=duckdb。"
        ) from exc

    return BaseDataSource(SimpleNamespace(data_bundle_path=path, future_info={}))


def is_suspended_on_date(code: str, as_of, *, bundle_path: str | None = None) -> bool:
    """与 RQAlpha 引擎一致：当日停牌则不可买卖。"""
    obid = to_rqalpha_id(code)
    if not obid:
        return False
    ds = get_rqalpha_data_source(bundle_path)
    day = pd.Timestamp(as_of).date()
    return bool(ds.is_suspended(obid, [day])[0])


def _bars_to_frame(bars) -> pd.DataFrame:
    if bars is None or len(bars) == 0:
        return pd.DataFrame(columns=["date", "close"])
    from rqalpha.utils.datetime_func import convert_int_to_date

    rows = []
    for bar in bars:
        rows.append(
            {
                "date": pd.Timestamp(convert_int_to_date(int(bar["datetime"]))),
                "close": float(bar["close"]),
            }
        )
    return pd.DataFrame(rows)


def load_kline_from_rqalpha(
    code: str,
    start: str,
    end: str,
    *,
    adjust_type: str = "none",
    bundle_path: str | None = None,
) -> pd.DataFrame | None:
    obid = to_rqalpha_id(code)
    if not obid:
        return None
    ds = get_rqalpha_data_source(bundle_path)
    try:
        inst = next(ds.get_instruments([obid]))
    except StopIteration:
        return None
    end_dt = datetime.fromisoformat(str(end)[:10])
    bars = ds.history_bars(
        inst,
        None,
        "1d",
        ["datetime", "close"],
        end_dt,
        skip_suspended=False,
        include_now=True,
        adjust_type=adjust_type,
        adjust_orig=end_dt,
    )
    df = _bars_to_frame(bars)
    if df.empty:
        return None
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    df = df[(df["date"] >= start_ts) & (df["date"] <= end_ts)].reset_index(drop=True)
    return df if not df.empty else None


def load_dividend_records_from_rqalpha(
    codes: list[str],
    *,
    bundle_path: str | None = None,
) -> pd.DataFrame:
    """从 RQAlpha bundle 构建分红记录（与 sys_accounts 派息同源）。"""
    from rqalpha.utils.datetime_func import convert_int_to_date

    ds = get_rqalpha_data_source(bundle_path)
    rows: list[dict] = []
    for code in codes:
        obid = to_rqalpha_id(code)
        if not obid:
            continue
        try:
            inst = next(ds.get_instruments([obid]))
        except StopIteration:
            continue
        divs = ds.get_dividend(inst)
        if divs is None or len(divs) == 0:
            continue
        nc = normalize_stock_code(code)
        for item in divs:
            lot = float(item["round_lot"]) or 10.0
            cash = float(item["dividend_cash_before_tax"]) / lot
            if cash <= 0:
                continue
            rows.append(
                {
                    "code": nc,
                    "ex_date": pd.Timestamp(convert_int_to_date(int(item["ex_dividend_date"]))),
                    "payable_date": pd.Timestamp(convert_int_to_date(int(item["payable_date"]))),
                    "cash_per_share": cash,
                }
            )
    if not rows:
        return pd.DataFrame(columns=["code", "ex_date", "payable_date", "cash_per_share"])
    return pd.DataFrame(rows).sort_values(["code", "ex_date"]).reset_index(drop=True)


def load_split_records_from_rqalpha(
    codes: list[str],
    *,
    bundle_path: str | None = None,
) -> pd.DataFrame:
    """从 RQAlpha bundle 读取送股/转增（与 sys_accounts 除权同源）。"""
    from rqalpha.utils.datetime_func import convert_int_to_date

    ds = get_rqalpha_data_source(bundle_path)
    rows: list[dict] = []
    for code in codes:
        obid = to_rqalpha_id(code)
        if not obid:
            continue
        try:
            inst = next(ds.get_instruments([obid]))
        except StopIteration:
            continue
        splits = ds.get_split(inst)
        if splits is None or len(splits) == 0:
            continue
        nc = normalize_stock_code(code)
        for item in splits:
            factor = float(item[1])
            if factor <= 1.0:
                continue
            rows.append(
                {
                    "code": nc,
                    "ex_date": pd.Timestamp(convert_int_to_date(int(item[0]))),
                    "factor": factor,
                }
            )
    if not rows:
        return pd.DataFrame(columns=["code", "ex_date", "factor"])
    return pd.DataFrame(rows).sort_values(["code", "ex_date"]).reset_index(drop=True)


def batch_load_klines_from_rqalpha(
    codes: list[str],
    start: str,
    end: str,
    *,
    adjust_type: str = "none",
    bundle_path: str | None = None,
) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for code in codes:
        nc = normalize_stock_code(code)
        df = load_kline_from_rqalpha(
            nc, start, end, adjust_type=adjust_type, bundle_path=bundle_path
        )
        if df is not None and not df.empty:
            out[nc] = df
    return out
