"""行业分类：申万一级（优先）+ 证监会（降级）。"""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from data_cache import is_fresh_today, load_dataframe, save_dataframe
from dividend_lowvol_rotation.config import (
    CACHE_DIR,
    INDUSTRY_CACHE_MAX_AGE_DAYS,
    INDUSTRY_SOURCE,
    SW_INDUSTRY_FETCH_SLEEP_SEC,
)
from dividend_lowvol_rotation.symbols import normalize_stock_code, to_baostock_code


def _cache_fresh(path: Path, max_age_days: int) -> bool:
    if not path.exists():
        return False
    if max_age_days <= 0:
        return is_fresh_today(path)
    modified = date.fromtimestamp(path.stat().st_mtime)
    return (date.today() - modified).days <= max_age_days


def _load_csrc_industry_table(refresh: bool = False) -> dict[str, str]:
    path = CACHE_DIR / "stock_industry_csrc.csv"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not refresh and _cache_fresh(path, INDUSTRY_CACHE_MAX_AGE_DAYS):
        cached = load_dataframe(path)
        if cached is not None and not cached.empty:
            return dict(zip(cached["code"], cached["industry"]))

    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        if path.exists():
            cached = load_dataframe(path)
            if cached is not None:
                return dict(zip(cached["code"], cached["industry"]))
        return {}
    rows = []
    try:
        rs = bs.query_stock_industry()
        while rs.error_code == "0" and rs.next():
            _update_date, bs_code, _name, industry, _classify = rs.get_row_data()
            code = bs_code.split(".")[-1] if bs_code else ""
            if not code:
                continue
            rows.append(
                {
                    "code": normalize_stock_code(code),
                    "industry": (industry or "").strip() or "未分类",
                    "source": "csrc",
                }
            )
    finally:
        bs.logout()

    if not rows:
        return {}
    df = pd.DataFrame(rows).drop_duplicates(subset=["code"], keep="last")
    save_dataframe(path, df)
    return dict(zip(df["code"], df["industry"]))


def _fetch_sw_industry_table(refresh: bool = False) -> dict[str, str]:
    path = CACHE_DIR / "stock_industry_sw_l1.csv"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not refresh and _cache_fresh(path, INDUSTRY_CACHE_MAX_AGE_DAYS):
        cached = load_dataframe(path)
        if cached is not None and not cached.empty:
            return dict(zip(cached["code"], cached["industry"]))

    import akshare as ak

    try:
        indices = ak.index_realtime_sw()
    except Exception:
        if path.exists():
            cached = load_dataframe(path)
            if cached is not None:
                return dict(zip(cached["code"], cached["industry"]))
        return {}

    if indices is None or indices.empty:
        return {}

    code_col = "指数代码" if "指数代码" in indices.columns else indices.columns[0]
    name_col = "指数名称" if "指数名称" in indices.columns else indices.columns[1]
    rows: list[dict] = []
    for _, idx_row in indices.iterrows():
        sw_code = str(idx_row[code_col]).strip()
        sw_name = str(idx_row[name_col]).strip()
        if not sw_code:
            continue
        try:
            cons = ak.index_component_sw(symbol=sw_code)
        except Exception:
            if SW_INDUSTRY_FETCH_SLEEP_SEC > 0:
                time.sleep(SW_INDUSTRY_FETCH_SLEEP_SEC)
            continue
        if cons is None or cons.empty:
            continue
        sym_col = "证券代码" if "证券代码" in cons.columns else cons.columns[1]
        for _, c_row in cons.iterrows():
            code = normalize_stock_code(c_row[sym_col])
            rows.append({"code": code, "industry": sw_name, "sw_code": sw_code, "source": "sw"})
        if SW_INDUSTRY_FETCH_SLEEP_SEC > 0:
            time.sleep(SW_INDUSTRY_FETCH_SLEEP_SEC)

    if not rows:
        return {}
    df = pd.DataFrame(rows).drop_duplicates(subset=["code"], keep="last")
    save_dataframe(path, df)
    return dict(zip(df["code"], df["industry"]))


def load_industry_table(refresh: bool = False) -> tuple[dict[str, str], str]:
    """返回 (code→行业名, 实际数据源标签)。"""
    source = INDUSTRY_SOURCE
    if source == "sw":
        mapping = _fetch_sw_industry_table(refresh=refresh)
        if mapping:
            return mapping, "申万一级"
        return _load_csrc_industry_table(refresh=refresh), "证监会(降级)"

    if source == "csrc":
        return _load_csrc_industry_table(refresh=refresh), "证监会"

    # sw_fallback
    mapping = _fetch_sw_industry_table(refresh=refresh)
    if mapping:
        return mapping, "申万一级"
    return _load_csrc_industry_table(refresh=refresh), "证监会(降级)"


def attach_industry(df: pd.DataFrame, refresh: bool = False) -> pd.DataFrame:
    mapping, source_label = load_industry_table(refresh=refresh)
    out = df.copy()
    out["industry"] = out["code"].map(lambda c: mapping.get(c, "未分类"))
    out["industry_source"] = source_label
    return out
