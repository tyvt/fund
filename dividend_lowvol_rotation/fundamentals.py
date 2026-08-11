"""基本面指标：fhps 字段 + 可选经营现金流质量。"""

from __future__ import annotations

import time

import pandas as pd

from dividend_lowvol_rotation.config import (
    FINANCIAL_FETCH_SLEEP_SEC,
    FUNDAMENTAL_FILTER_ENABLED,
    MIN_OCF_TO_PROFIT,
    MIN_PROFIT_YOY_PCT,
    MIN_ROE_PCT,
    OCF_QUALITY_FILTER_ENABLED,
)


def roe_from_eps_bps(eps, bps) -> float | None:
    try:
        eps_f = float(eps)
        bps_f = float(bps)
    except (TypeError, ValueError):
        return None
    if bps_f <= 0:
        return None
    return eps_f / bps_f * 100.0


def attach_fundamentals_from_fhps(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "eps" in out.columns and "bps" in out.columns:
        out["roe_pct"] = out.apply(
            lambda r: roe_from_eps_bps(r.get("eps"), r.get("bps")), axis=1
        )
    if "profit_yoy_pct" not in out.columns:
        out["profit_yoy_pct"] = None
    return out


def fundamental_filter_mask(df: pd.DataFrame) -> pd.Series:
    if not FUNDAMENTAL_FILTER_ENABLED:
        return pd.Series(True, index=df.index)
    ok = pd.Series(True, index=df.index)
    if "roe_pct" in df.columns:
        ok &= df["roe_pct"].notna() & (df["roe_pct"] >= MIN_ROE_PCT)
    if "profit_yoy_pct" in df.columns:
        ok &= df["profit_yoy_pct"].isna() | (df["profit_yoy_pct"] >= MIN_PROFIT_YOY_PCT)
    return ok


def _fetch_ocf_to_profit(code: str) -> float | None:
    import akshare as ak

    try:
        fa = ak.stock_financial_abstract(symbol=code)
    except Exception:
        return None
    if fa is None or fa.empty:
        return None
    row = fa[fa["指标"] == "经营活动净现金/归属母公司的净利润"]
    if row.empty:
        return None
    # 取最新一期非空列
    series = row.iloc[0].drop(labels=["选项", "指标"])
    for val in series:
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        if pd.notna(v):
            return v
    return None


def attach_ocf_quality(df: pd.DataFrame, refresh: bool = False) -> pd.DataFrame:
    if not OCF_QUALITY_FILTER_ENABLED:
        return df
    out = df.copy()
    ratios = []
    for i, code in enumerate(out["code"]):
        ratios.append(_fetch_ocf_to_profit(code))
        if FINANCIAL_FETCH_SLEEP_SEC > 0 and i + 1 < len(out):
            time.sleep(FINANCIAL_FETCH_SLEEP_SEC)
    out["ocf_to_profit"] = ratios
    return out


def ocf_filter_mask(df: pd.DataFrame) -> pd.Series:
    if not OCF_QUALITY_FILTER_ENABLED or "ocf_to_profit" not in df.columns:
        return pd.Series(True, index=df.index)
    return df["ocf_to_profit"].isna() | (df["ocf_to_profit"] >= MIN_OCF_TO_PROFIT)
