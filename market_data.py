"""公共行情数据拉取（国债、指数历史等）。"""

import sys
from datetime import date

import numpy as np
import pandas as pd
import requests

from config import (
    BOND_HISTORY_MAX_PAGES,
    BOND_HISTORY_PAGE_SIZE,
    BOND_REQUEST_TIMEOUT,
    BOND_YIELD_FALLBACK_BY_YEAR,
    BOND_YIELD_FIELD,
    BOND_YIELD_PARAMS,
    BOND_YIELD_URL,
    DIVIDEND_TOTAL_RETURN_INDEX,
    HEADERS,
    INDEX_PERF_URL,
    REQUEST_TIMEOUT,
    indicator_xls_url,
)
from data_cache import get_or_fetch_dataframe, get_or_fetch_json


def configure_stdout_utf8():
    """Windows 终端 UTF-8 输出。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def _fetch_indicator_history(index_code):
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


_TOTAL_RETURN_CODES = frozenset(DIVIDEND_TOTAL_RETURN_INDEX.values())


def read_indicator_history(index_code):
    """读取中证指数指标文件中的近期 PE 与股息率。"""
    if index_code in _TOTAL_RETURN_CODES:
        return None
    try:
        return get_or_fetch_dataframe(
            f"indicator_{index_code}",
            lambda: _fetch_indicator_history(index_code),
            subdir="cn",
        )
    except Exception as exc:
        print(f" 读取 {index_code} 指标文件时出错: {exc}")
        return None


def _fetch_index_perf_history(index_code, start_date, end_date):
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
        print(f" 无法获取 {index_code} 在 {start_date}-{end_date} 的历史数据。")
        return None

    history = pd.DataFrame(records)
    history["date"] = pd.to_datetime(history["tradeDate"]).dt.date
    history["rolling_pe"] = pd.to_numeric(history["peg"], errors="coerce")
    history["close"] = pd.to_numeric(history["close"], errors="coerce")
    history["trading_value"] = pd.to_numeric(history["tradingValue"], errors="coerce")
    history = history.dropna(subset=["date", "close"])
    return history.sort_values("date").reset_index(drop=True)


def load_index_perf_history(index_code, force=False):
    """加载指数全历史 perf（本地缓存 + 增量补齐）。"""
    from data_cache import cache_path, is_fresh_today, load_dataframe, merge_dataframes_by_date, save_dataframe
    from index_meta import get_index_base_date

    key = f"index_perf_{index_code}"
    path = cache_path(key, subdir="cn")
    cached = load_dataframe(path)
    if cached is not None and not cached.empty:
        cached = cached.copy()
        cached["date"] = pd.to_datetime(cached["date"]).dt.date

    if not force and is_fresh_today(path) and cached is not None and not cached.empty:
        return cached

    today = date.today().strftime("%Y%m%d")
    base = get_index_base_date(index_code) or "19900101"
    segments: list[tuple[str, str]] = []

    if cached is None or cached.empty:
        segments.append((base, today))
    else:
        dmin = pd.to_datetime(cached["date"]).min().date()
        dmax = pd.to_datetime(cached["date"]).max().date()
        base_d = pd.Timestamp(base).date()
        if base_d < dmin:
            prev = (pd.Timestamp(dmin) - pd.Timedelta(days=1)).strftime("%Y%m%d")
            segments.append((base, prev))
        if dmax < date.today():
            nxt = (pd.Timestamp(dmax) + pd.Timedelta(days=1)).strftime("%Y%m%d")
            segments.append((nxt, today))

    merged = cached
    for seg_start, seg_end in segments:
        if seg_start > seg_end:
            continue
        try:
            chunk = _fetch_index_perf_history(index_code, seg_start, seg_end)
            if chunk is not None and not chunk.empty:
                chunk = chunk.copy()
                chunk["date"] = pd.to_datetime(chunk["date"]).dt.date
                merged = merge_dataframes_by_date(merged, chunk, date_col="date")
        except Exception as exc:
            print(f" 获取 {index_code} {seg_start}-{seg_end} 时出错: {exc}")

    if merged is not None and not merged.empty:
        save_dataframe(path, merged)
        return merged
    return cached


def get_index_perf_history(index_code, start_date=None, end_date=None, years=10):
    """从中证指数 API 获取历史行情与滚动 PE（优先读本地全量缓存）。"""
    if end_date is None:
        end_date = date.today().strftime("%Y%m%d")

    full = load_index_perf_history(index_code)
    if full is None or full.empty:
        return None

    out = full.copy()
    out["_dt"] = pd.to_datetime(out["date"])
    if start_date is not None:
        out = out[out["_dt"] >= pd.Timestamp(start_date)]
    elif years is not None:
        cutoff = pd.Timestamp(date.today()) - pd.DateOffset(years=years)
        out = out[out["_dt"] >= cutoff]
    if end_date is not None:
        out = out[out["_dt"] <= pd.Timestamp(end_date)]
    return out.drop(columns="_dt").reset_index(drop=True)


def _fetch_gov_bond_yield_history():
    """东方财富国债收益率：接口单次最多 500 条，需分页拉取全历史。"""
    page_size = BOND_HISTORY_PAGE_SIZE
    all_records: list[dict] = []
    for page in range(1, BOND_HISTORY_MAX_PAGES + 1):
        params = {
            **BOND_YIELD_PARAMS,
            "ps": str(page_size),
            "p": str(page),
            "pageNo": str(page),
            "pageNum": str(page),
        }
        response = requests.get(
            BOND_YIELD_URL,
            params=params,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        payload = response.json() or {}
        result = payload.get("result") or {}
        records = result.get("data") or []
        if not records:
            break
        all_records.extend(records)
        if len(records) < page_size:
            break

    if not all_records:
        print(" 无法从接口获取国债收益率历史。")
        return None

    history = pd.DataFrame(all_records).drop_duplicates(subset=["SOLAR_DATE"])
    history["date"] = pd.to_datetime(history["SOLAR_DATE"]).dt.date
    history["bond_yield"] = (
        pd.to_numeric(history[BOND_YIELD_FIELD], errors="coerce") / 100
    )
    history = history.dropna(subset=["date", "bond_yield"])
    return history.sort_values("date").reset_index(drop=True)


def get_gov_bond_yield_history():
    """从东方财富获取国债收益率历史（分页拉取，自动升级旧版短缓存）。"""
    from data_cache import cache_path, load_dataframe, save_dataframe

    path = cache_path("bond_yield_history", subdir="cn")
    cached = load_dataframe(path)
    if cached is not None and len(cached) < 2000:
        try:
            fresh = _fetch_gov_bond_yield_history()
            if fresh is not None and len(fresh) > len(cached):
                save_dataframe(path, fresh)
        except Exception as exc:
            print(f" 升级国债收益率缓存时出错: {exc}")

    try:
        history = get_or_fetch_dataframe(
            "bond_yield_history",
            _fetch_gov_bond_yield_history,
            subdir="cn",
        )
        return _normalize_bond_history(history)
    except Exception as exc:
        print(f" 获取国债收益率历史时出错: {exc}")
        return None


def get_gov_bond_yield():
    """获取最新 10 年期国债收益率。"""
    def _fetch():
        response = requests.get(
            BOND_YIELD_URL,
            params=BOND_YIELD_PARAMS,
            headers=HEADERS,
            timeout=BOND_REQUEST_TIMEOUT,
        )
        records = response.json().get("result", {}).get("data", [])
        if not records:
            raise RuntimeError("无法从接口获取国债收益率")
        latest = records[0]
        bond_yield = float(latest[BOND_YIELD_FIELD]) / 100
        data_date = pd.to_datetime(latest["SOLAR_DATE"]).date().isoformat()
        return {"bond_yield": bond_yield, "data_date": data_date}

    try:
        data = get_or_fetch_json("bond_yield_latest", _fetch, subdir="cn")
        return data["bond_yield"], date.fromisoformat(data["data_date"])
    except Exception as exc:
        print(f" 获取国债收益率时出错: {exc}")
        return None, None


def compute_percentile(series, value):
    """计算 value 在 series 中的历史分位（越低通常越便宜）。"""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    values = pd.to_numeric(pd.Series(series), errors="coerce").dropna().to_numpy(dtype=float)
    if values.size == 0:
        return None
    return float(np.mean(values < value) * 100)


def rolling_percentile_series(series, window, min_periods=None):
    """滚动历史分位：当前值相对 [i-window, i) 窗口内样本的分位（0-100）。"""
    values = pd.to_numeric(pd.Series(series), errors="coerce").to_numpy(dtype=float)
    n = len(values)
    if n == 0:
        return np.array([], dtype=float)
    min_periods = window if min_periods is None else min_periods
    out = np.full(n, np.nan)
    for i in range(n):
        if i < min_periods:
            continue
        v = values[i]
        if np.isnan(v):
            continue
        start = max(0, i - window)
        hist = values[start:i]
        hist = hist[~np.isnan(hist)]
        if hist.size == 0:
            continue
        out[i] = float(np.mean(hist < v) * 100)
    return out


def rolling_window_stats(series, window, min_periods):
    """滚动窗口 min/max/分位/样本数（窗口为 [i-window, i)，不含当前点）。"""
    values = pd.to_numeric(pd.Series(series), errors="coerce").to_numpy(dtype=float)
    n = len(values)
    mins = np.full(n, np.nan)
    maxs = np.full(n, np.nan)
    pcts = np.full(n, np.nan)
    samples = np.full(n, np.nan)
    for i in range(n):
        if i < min_periods:
            continue
        start = max(0, i - window)
        hist = values[start:i]
        hist = hist[~np.isnan(hist)]
        if hist.size == 0:
            continue
        v = values[i]
        samples[i] = float(hist.size)
        mins[i] = float(hist.min())
        maxs[i] = float(hist.max())
        if not np.isnan(v):
            pcts[i] = float(np.mean(hist < v) * 100)
    return mins, maxs, pcts, samples


def asof_datetime(series):
    """日期列转为 datetime64[ns]，供 merge_asof 对齐（不接受 object dtype）。"""
    return pd.to_datetime(series, errors="coerce").dt.normalize()


def _to_date(value):
    """统一转为 datetime.date 便于对齐。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return pd.Timestamp(value).date()


def _normalize_bond_history(bond_history):
    if bond_history is None or bond_history.empty:
        return bond_history
    out = bond_history.copy()
    out["date"] = out["date"].map(_to_date)
    return out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def resolve_bond_yield_for_date(target_date, bond_history=None):
    """优先使用日度国债；缺失时按年度回填。"""
    day = _to_date(target_date)
    if day is None:
        return None
    if bond_history is not None and not bond_history.empty:
        bond = _normalize_bond_history(bond_history)
        matched = bond.loc[bond["date"] == day, "bond_yield"]
        if not matched.empty:
            return float(matched.iloc[0])
    return BOND_YIELD_FALLBACK_BY_YEAR.get(day.year)


def attach_bond_yield(panel, bond_history=None):
    """将国债收益率对齐到指数面板（日度 merge_asof + 年度回填）。"""
    if panel is None or panel.empty:
        return panel
    out = panel.copy()
    out["_date_dt"] = asof_datetime(out["date"])

    if bond_history is not None and not bond_history.empty:
        bond = _normalize_bond_history(bond_history)
        bond["_date_dt"] = asof_datetime(bond["date"])
        out = pd.merge_asof(
            out.sort_values("_date_dt"),
            bond[["_date_dt", "bond_yield"]].sort_values("_date_dt"),
            on="_date_dt",
            direction="backward",
        )
    else:
        out["bond_yield"] = pd.NA

    missing = out["bond_yield"].isna()
    if missing.any():
        years = pd.to_datetime(out.loc[missing, "date"]).dt.year
        out.loc[missing, "bond_yield"] = years.map(
            lambda y: BOND_YIELD_FALLBACK_BY_YEAR.get(int(y))
        )

    out = out.drop(columns=["_date_dt"], errors="ignore")
    return out.dropna(subset=["bond_yield"])


def merge_index_with_bond(index_history, bond_history):
    """按交易日对齐指数与国债收益率。"""
    merged = attach_bond_yield(index_history, bond_history)
    if "bond_yield" in merged.columns:
        merged["bond_yield"] = merged["bond_yield"].ffill()
    return merged.dropna(subset=["bond_yield"])
