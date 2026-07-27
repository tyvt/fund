"""公共行情数据拉取（国债、指数历史等）。"""

import sys
from datetime import date
from functools import lru_cache

import pandas as pd
import requests

from config import (
    BOND_HISTORY_PAGE_SIZE,
    BOND_REQUEST_TIMEOUT,
    BOND_YIELD_FALLBACK_BY_YEAR,
    BOND_YIELD_FIELD,
    BOND_YIELD_PARAMS,
    BOND_YIELD_URL,
    HEADERS,
    INDEX_PERF_URL,
    REQUEST_TIMEOUT,
    indicator_xls_url,
)


def configure_stdout_utf8():
    """Windows 终端 UTF-8 输出。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


@lru_cache(maxsize=16)
def read_indicator_history(index_code):
    """读取中证指数指标文件中的近期 PE 与股息率。"""
    try:
        df = pd.read_excel(indicator_xls_url(index_code))
        if df.empty:
            return None
        out = df.rename(
            columns={
                "日期Date": "date",
                "市盈率1（总股本）P/E1": "pe",
                "市盈率2（计算用股本）P/E2": "pe2",
                "股息率1（总股本）D/P1": "dividend_yield",
                "股息率2（计算用股本）D/P2": "dividend_yield2",
            }
        )
        out["date"] = pd.to_datetime(
            out["date"].astype(str), format="%Y%m%d", errors="coerce"
        ).dt.date
        out["pe"] = pd.to_numeric(out["pe"], errors="coerce")
        out["dividend_yield"] = pd.to_numeric(out["dividend_yield"], errors="coerce") / 100
        return out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    except Exception as exc:
        print(f" 读取 {index_code} 指标文件时出错: {exc}")
        return None


@lru_cache(maxsize=32)
def get_index_perf_history(index_code, start_date=None, end_date=None, years=10):
    """从中证指数 API 获取历史行情与滚动 PE。"""
    if end_date is None:
        end_date = date.today().strftime("%Y%m%d")
    if start_date is None:
        start_date = f"{date.today().year - years}0101"

    try:
        response = requests.get(
            INDEX_PERF_URL,
            params={
                "indexCode": index_code,
                "startDate": start_date,
                "endDate": end_date,
            },
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        payload = response.json()
        records = payload.get("data") or []
        if not records:
            print(
                f" 无法获取 {index_code} 在 {start_date}-{end_date} 的历史数据。"
            )
            return None

        history = pd.DataFrame(records)
        history["date"] = pd.to_datetime(history["tradeDate"]).dt.date
        history["rolling_pe"] = pd.to_numeric(history["peg"], errors="coerce")
        history["close"] = pd.to_numeric(history["close"], errors="coerce")
        history["trading_value"] = pd.to_numeric(
            history["tradingValue"], errors="coerce"
        )
        history = history.dropna(subset=["date", "close"])
        return history.sort_values("date").reset_index(drop=True)
    except Exception as exc:
        print(f" 获取 {index_code} 历史数据时出错: {exc}")
        return None


@lru_cache(maxsize=1)
def get_gov_bond_yield_history():
    """从东方财富获取国债收益率历史。"""
    try:
        params = {**BOND_YIELD_PARAMS, "ps": str(BOND_HISTORY_PAGE_SIZE)}
        response = requests.get(
            BOND_YIELD_URL,
            params=params,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        records = response.json().get("result", {}).get("data", [])
        if not records:
            print(" 无法从接口获取国债收益率历史。")
            return None

        history = pd.DataFrame(records)
        history["date"] = pd.to_datetime(history["SOLAR_DATE"]).dt.date
        history["bond_yield"] = (
            pd.to_numeric(history[BOND_YIELD_FIELD], errors="coerce") / 100
        )
        history = history.dropna(subset=["date", "bond_yield"])
        return history.sort_values("date").reset_index(drop=True)
    except Exception as exc:
        print(f" 获取国债收益率历史时出错: {exc}")
        return None


def get_gov_bond_yield():
    """获取最新 10 年期国债收益率。"""
    try:
        response = requests.get(
            BOND_YIELD_URL,
            params=BOND_YIELD_PARAMS,
            headers=HEADERS,
            timeout=BOND_REQUEST_TIMEOUT,
        )
        records = response.json().get("result", {}).get("data", [])
        if not records:
            print(" 无法从接口获取国债收益率。")
            return None, None
        latest = records[0]
        bond_yield = float(latest[BOND_YIELD_FIELD]) / 100
        data_date = pd.to_datetime(latest["SOLAR_DATE"]).date()
        return bond_yield, data_date
    except Exception as exc:
        print(f" 获取国债收益率时出错: {exc}")
        return None, None


def compute_percentile(series, value):
    """计算 value 在 series 中的历史分位（越低通常越便宜）。"""
    values = pd.Series(series).dropna()
    if values.empty or value is None:
        return None
    return float((values < value).mean() * 100)


def resolve_bond_yield_for_date(target_date, bond_history=None):
    """优先使用日度国债；缺失时按年度回填。"""
    if bond_history is not None and not bond_history.empty:
        matched = bond_history.loc[bond_history["date"] == target_date, "bond_yield"]
        if not matched.empty:
            return float(matched.iloc[0])
    return BOND_YIELD_FALLBACK_BY_YEAR.get(target_date.year)


def merge_index_with_bond(index_history, bond_history):
    """按交易日对齐指数与国债收益率。"""
    merged = index_history.merge(
        bond_history[["date", "bond_yield"]],
        on="date",
        how="left",
    )
    merged["bond_yield"] = merged["bond_yield"].ffill()
    return merged.dropna(subset=["bond_yield"])
