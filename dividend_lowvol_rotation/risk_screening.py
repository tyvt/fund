# -*- coding: utf-8 -*-
"""排雷因子：现金流质量、ROE 稳定性、分红连续性、支付率、负债率、利息保障。"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from data_cache import is_fresh_today, save_dataframe
from dividend_lowvol_rotation.config import (
    CACHE_DIR,
    DEBT_RATIO_INDUSTRY_MARGIN_PCT,
    DEBT_RATIO_INDUSTRY_NEUTRAL,
    FINANCIAL_FETCH_SLEEP_SEC,
    MAX_DEBT_RATIO_PCT,
    MAX_PAYOUT_RATIO_PCT,
    MAX_ROE_VOLATILITY_RATIO,
    MIN_DIVIDEND_YEARS,
    MIN_INTEREST_COVERAGE,
    MIN_OCF_TO_PROFIT,
    MIN_PAYOUT_RATIO_PCT,
    OCF_QUALITY_FILTER_ENABLED,
    RISK_FILTER_ENABLED,
    RISK_LOOKBACK_YEARS,
    RISK_PENALTY_DEBT,
    RISK_PENALTY_DIVIDEND_YEARS,
    RISK_PENALTY_INTEREST,
    RISK_PENALTY_OCF,
    RISK_PENALTY_PAYOUT,
    RISK_PENALTY_ROE_VOL,
    ROE_VOL_INDUSTRY_NEUTRAL,
    SOFT_RISK_SCORING_ENABLED,
)
from dividend_lowvol_rotation.symbols import normalize_stock_code


def _risk_hist_path(code: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"risk_hist_{normalize_stock_code(code)}.csv"


def _read_risk_csv(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        return pd.read_csv(path)
    except Exception:
        return None


def _parse_cn_amount(val) -> float | None:
    if val is None or val is False or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip().replace(",", "")
    if not s or s.lower() == "false":
        return None
    mult = 1.0
    if s.endswith("亿"):
        mult = 1e8
        s = s[:-1]
    elif s.endswith("万"):
        mult = 1e4
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def _date_cols(df: pd.DataFrame) -> list[tuple[pd.Timestamp, str]]:
    out: list[tuple[pd.Timestamp, str]] = []
    for col in df.columns:
        if col in ("选项", "指标"):
            continue
        try:
            out.append((pd.Timestamp(str(col)), str(col)))
        except ValueError:
            continue
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def _row_values_by_dates(df: pd.DataFrame, indicator: str) -> dict[pd.Timestamp, float]:
    sub = df[df["指标"] == indicator]
    if sub.empty:
        return {}
    row = sub.iloc[0]
    out: dict[pd.Timestamp, float] = {}
    for ts, col in _date_cols(df):
        try:
            v = float(row[col])
        except (TypeError, ValueError):
            continue
        if pd.notna(v):
            out[ts] = v
    return out


def _annual_year_end_values(values: dict[pd.Timestamp, float]) -> dict[int, float]:
    by_year: dict[int, tuple[pd.Timestamp, float]] = {}
    for ts, v in values.items():
        y = ts.year
        if ts.month == 12 and ts.day == 31:
            by_year[y] = (ts, v)
        elif y not in by_year or ts > by_year[y][0]:
            if y not in by_year or ts.month == 12:
                by_year[y] = (ts, v)
    return {y: pair[1] for y, pair in by_year.items()}


def _roe_volatility_ratio(roe_by_year: dict[int, float], lookback: int = RISK_LOOKBACK_YEARS) -> float | None:
    if not roe_by_year:
        return None
    years = sorted(roe_by_year.keys(), reverse=True)[:lookback]
    roes = [roe_by_year[y] for y in years if roe_by_year.get(y) is not None]
    if len(roes) < 3:
        return None
    mean = float(np.mean(roes))
    if mean <= 0:
        return None
    return float(np.std(roes) / mean)


def _roe_volatility_ratio_as_of(
    roe_by_year: dict[int, float],
    up_to_year: int,
    lookback: int = RISK_LOOKBACK_YEARS,
) -> float | None:
    """仅用 report_year <= up_to_year 的 ROE 计算波动率，避免未来信息。"""
    trimmed = {y: v for y, v in roe_by_year.items() if y <= up_to_year and v is not None}
    return _roe_volatility_ratio(trimmed, lookback=lookback)


def _enrich_hist_roe_vol_as_of(hist: pd.DataFrame) -> pd.DataFrame:
    """按各行 report_year 重算 roe_volatility_ratio（读缓存/快照时修复旧数据）。"""
    if hist.empty or "report_year" not in hist.columns or "roe_pct" not in hist.columns:
        return hist
    out = hist.copy()
    roe_by_code: dict[str, dict[int, float]] = {}
    for _, row in out.iterrows():
        code = str(row["code"])
        y = int(row["report_year"])
        roe = pd.to_numeric(row.get("roe_pct"), errors="coerce")
        if pd.notna(roe):
            roe_by_code.setdefault(code, {})[y] = float(roe)
    ratios: list[float | None] = []
    for _, row in out.iterrows():
        code = str(row["code"])
        y = int(row["report_year"])
        ratios.append(_roe_volatility_ratio_as_of(roe_by_code.get(code, {}), y))
    out["roe_volatility_ratio"] = ratios
    return out


def _fetch_interest_coverage_akshare(code: str, fa: pd.DataFrame | None) -> dict[pd.Timestamp, float]:
    """利息保障倍数：优先 akshare 财务摘要，其次利润表 EM 口径。"""
    if fa is not None and not fa.empty:
        icov = _row_values_by_dates(fa, "利息保障倍数")
        if icov:
            return icov

    import akshare as ak

    try:
        df = ak.stock_profit_sheet_by_report_em(symbol=code)
    except Exception:
        return {}
    if df is None or df.empty:
        return {}

    date_col = None
    for c in df.columns:
        if "报告日" in str(c) or c == "REPORT_DATE":
            date_col = c
            break
    if date_col is None:
        date_col = df.columns[0]

    op_col = None
    fin_col = None
    for c in df.columns:
        cs = str(c)
        if op_col is None and ("营业利润" in cs or "OPERATE_PROFIT" in cs):
            op_col = c
        if fin_col is None and ("财务费用" in cs or "FINANCE_EXPENSE" in cs):
            fin_col = c
    if op_col is None or fin_col is None:
        return {}

    out: dict[pd.Timestamp, float] = {}
    for _, row in df.iterrows():
        try:
            ts = pd.Timestamp(str(row[date_col]))
        except Exception:
            continue
        op = _parse_cn_amount(row[op_col])
        fin = _parse_cn_amount(row[fin_col])
        if op is None or fin is None or fin <= 0:
            continue
        out[ts] = op / fin
    return out


def fetch_risk_history(code: str, refresh: bool = False) -> pd.DataFrame:
    code = normalize_stock_code(code)
    path = _risk_hist_path(code)
    if not refresh and path.exists() and is_fresh_today(path):
        cached = _read_risk_csv(path)
        if cached is not None and not cached.empty:
            return cached

    import akshare as ak

    rows: list[dict] = []
    try:
        fa = ak.stock_financial_abstract(symbol=code)
    except Exception:
        fa = None

    roe_ann: dict[int, float] = {}
    debt_ann: dict[int, float] = {}
    profit_ann: dict[int, float] = {}
    ocf_ann: dict[int, float] = {}
    if fa is not None and not fa.empty:
        roe_ann = _annual_year_end_values(_row_values_by_dates(fa, "净资产收益率(ROE)"))
        debt_ann = _annual_year_end_values(_row_values_by_dates(fa, "资产负债率"))
        profit_ann = _annual_year_end_values(_row_values_by_dates(fa, "净利润"))
        ocf_ann = _annual_year_end_values(
            _row_values_by_dates(fa, "经营活动净现金/归属母公司的净利润")
        )
        try:
            from dividend_lowvol_rotation.enhanced_factors import cache_quarterly_profits_from_fa

            cache_quarterly_profits_from_fa(code, fa)
        except Exception:
            pass

    icov = _fetch_interest_coverage_akshare(code, fa)
    icov_ann: dict[int, float] = {}
    for ts, v in icov.items():
        y = ts.year
        if ts.month == 12 or y not in icov_ann:
            icov_ann[y] = v

    all_years = sorted(set(roe_ann) | set(debt_ann) | set(ocf_ann) | set(icov_ann) | set(profit_ann), reverse=True)
    for y in all_years:
        rows.append(
            {
                "code": code,
                "report_year": y,
                "report_date": f"{y}-12-31",
                "roe_pct": roe_ann.get(y),
                "debt_ratio_pct": debt_ann.get(y),
                "net_profit": profit_ann.get(y),
                "ocf_to_profit": ocf_ann.get(y),
                "interest_coverage": icov_ann.get(y),
            }
        )

    if not rows:
        return pd.DataFrame()

    hist = pd.DataFrame(rows)
    hist = _enrich_hist_roe_vol_as_of(hist)
    save_dataframe(path, hist)
    return hist


def _merged_risk_hist_path() -> Path:
    return CACHE_DIR / "risk_hist_merged.csv"


def _load_merged_risk_hist() -> pd.DataFrame | None:
    return _read_risk_csv(_merged_risk_hist_path())


def batch_load_risk_history(codes: list[str], refresh: bool = False) -> pd.DataFrame:
    unique = list(dict.fromkeys(normalize_stock_code(c) for c in codes))
    if not unique:
        return pd.DataFrame()

    merged: pd.DataFrame | None = None
    if not refresh:
        merged = _load_merged_risk_hist()

    have: set[str] = set()
    if merged is not None and not merged.empty:
        merged["code"] = merged["code"].map(normalize_stock_code)
        have = set(merged["code"].unique())

    need = [c for c in unique if c not in have]
    new_frames: list[pd.DataFrame] = []
    if need:
        print(f"  排雷数据：{len(unique)} 只，需拉取 {len(need)} 只…", flush=True)
    for i, code in enumerate(need):
        path = _risk_hist_path(code)
        if not refresh and path.exists():
            cached = _read_risk_csv(path)
            if cached is not None and not cached.empty:
                new_frames.append(cached)
                continue
        hist = fetch_risk_history(code, refresh=refresh)
        if hist is not None and not hist.empty:
            new_frames.append(hist)
        if need and (i + 1) % 25 == 0:
            print(f"    排雷拉取 {i + 1}/{len(need)}…", flush=True)
        if FINANCIAL_FETCH_SLEEP_SEC > 0 and i + 1 < len(need):
            time.sleep(FINANCIAL_FETCH_SLEEP_SEC)

    if new_frames:
        new_df = pd.concat(new_frames, ignore_index=True, sort=False)
        new_df["code"] = new_df["code"].map(normalize_stock_code)
        if merged is not None and not merged.empty:
            merged = pd.concat([merged, new_df], ignore_index=True, sort=False)
        else:
            merged = new_df
        merged = merged.drop_duplicates(subset=["code", "report_year"], keep="last")
        save_dataframe(_merged_risk_hist_path(), merged)

    if merged is None or merged.empty:
        frames: list[pd.DataFrame] = []
        for code in unique:
            path = _risk_hist_path(code)
            if not path.exists():
                continue
            cached = _read_risk_csv(path)
            if cached is not None and not cached.empty:
                frames.append(cached)
        if not frames:
            return pd.DataFrame()
        merged = pd.concat(frames, ignore_index=True, sort=False)
        merged["code"] = merged["code"].map(normalize_stock_code)

    merged = _enrich_hist_roe_vol_as_of(merged)
    return merged[merged["code"].isin(unique)].copy()


def risk_snapshot_as_of(risk_hist: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    if risk_hist is None or risk_hist.empty:
        return pd.DataFrame()
    sub = risk_hist[risk_hist["report_year"] <= as_of.year].copy()
    if sub.empty:
        return pd.DataFrame()
    sub["code"] = sub["code"].map(normalize_stock_code)
    idx = sub.groupby("code")["report_year"].idxmax()
    snap = sub.loc[idx].set_index("code")
    cols = [
        "roe_pct",
        "debt_ratio_pct",
        "ocf_to_profit",
        "interest_coverage",
        "roe_volatility_ratio",
    ]
    keep = [c for c in cols if c in snap.columns]
    return snap[keep]


class DividendYearIndex:
    def __init__(self, records: pd.DataFrame) -> None:
        self._dates: dict[str, list[pd.Timestamp]] = {}
        self._window_cache: dict[tuple[str, int], dict[str, int]] = {}
        if records is None or records.empty:
            return
        pos = records[records["cash_per_share"] > 0].copy()
        if pos.empty:
            return
        pos["code"] = pos["code"].map(normalize_stock_code)
        for code, grp in pos.groupby("code"):
            self._dates[str(code)] = sorted(pd.to_datetime(grp["ex_date"]).tolist())

    def years_map_at(
        self,
        as_of: pd.Timestamp,
        *,
        window_years: int = RISK_LOOKBACK_YEARS,
    ) -> dict[str, int]:
        key = (as_of.date().isoformat(), window_years)
        cached = self._window_cache.get(key)
        if cached is not None:
            return cached
        start_year = as_of.year - window_years + 1
        years_range = range(start_year, as_of.year + 1)
        out: dict[str, int] = {}
        for code, dates in self._dates.items():
            years_with_div = {d.year for d in dates if d <= as_of}
            out[code] = sum(1 for y in years_range if y in years_with_div)
        self._window_cache[key] = out
        return out


def build_dividend_year_index(records: pd.DataFrame) -> DividendYearIndex:
    return DividendYearIndex(records)


def attach_risk_from_records(
    df: pd.DataFrame,
    records: pd.DataFrame,
    as_of: pd.Timestamp | None = None,
    *,
    div_index: DividendYearIndex | None = None,
) -> pd.DataFrame:
    today = as_of or pd.Timestamp(date.today())
    out = df.copy()
    if "eps" in out.columns and "cash_per_share" in out.columns:
        eps = pd.to_numeric(out["eps"], errors="coerce")
        cash = pd.to_numeric(out["cash_per_share"], errors="coerce")
        out["payout_ratio_pct"] = np.where(eps > 0, cash / eps * 100.0, np.nan)
    else:
        out["payout_ratio_pct"] = None

    idx = div_index or build_dividend_year_index(records)
    years_map = idx.years_map_at(today)
    out["dividend_years_5y"] = out["code"].map(
        lambda c: years_map.get(normalize_stock_code(str(c)), 0)
    )
    return out


def merge_risk_history(
    df: pd.DataFrame,
    risk_hist: pd.DataFrame,
    as_of: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if df.empty:
        return df
    today = as_of or pd.Timestamp(date.today())
    snap = risk_snapshot_as_of(risk_hist, today)
    if snap.empty:
        return df
    out = df.copy()
    out["code"] = out["code"].map(normalize_stock_code)
    return out.merge(snap, on="code", how="left", suffixes=("", "_risk"))


def _industry_col(df: pd.DataFrame) -> pd.Series:
    if "industry" in df.columns:
        return df["industry"].fillna("未分类").astype(str)
    return pd.Series("未分类", index=df.index)


def risk_score_penalties(
    df: pd.DataFrame,
    *,
    skip: dict[str, bool] | None = None,
) -> tuple[pd.Series, dict[str, int]]:
    """排雷因子评分扣减（越大越差）；用于软性筛选替代硬性剔除。"""
    stats: dict[str, int] = {}
    skip = skip or {}
    penalty = pd.Series(0.0, index=df.index, dtype=float)
    if not RISK_FILTER_ENABLED or df.empty:
        return penalty, stats

    industry = _industry_col(df)

    if OCF_QUALITY_FILTER_ENABLED and not skip.get("ocf") and "ocf_to_profit" in df.columns:
        bad = df["ocf_to_profit"].notna() & (df["ocf_to_profit"] < MIN_OCF_TO_PROFIT)
        stats["ocf_penalized"] = int(bad.sum())
        gap = (MIN_OCF_TO_PROFIT - df["ocf_to_profit"]).clip(lower=0)
        penalty = penalty + bad.astype(float) * (RISK_PENALTY_OCF + gap * 2.0)

    if not skip.get("roe_vol") and "roe_volatility_ratio" in df.columns:
        if ROE_VOL_INDUSTRY_NEUTRAL:
            ind_mean = df.groupby(industry)["roe_volatility_ratio"].transform("mean")
            excess = (df["roe_volatility_ratio"] - ind_mean).clip(lower=0)
            bad = df["roe_volatility_ratio"].notna() & ind_mean.notna() & (excess > 0)
        else:
            excess = (df["roe_volatility_ratio"] - MAX_ROE_VOLATILITY_RATIO).clip(lower=0)
            bad = df["roe_volatility_ratio"].notna() & (excess > 0)
        stats["roe_vol_penalized"] = int(bad.sum())
        penalty = penalty + bad.astype(float) * (RISK_PENALTY_ROE_VOL + excess.fillna(0) * 5.0)

    if not skip.get("dividend_years") and "dividend_years_5y" in df.columns:
        short = (MIN_DIVIDEND_YEARS - df["dividend_years_5y"]).clip(lower=0)
        bad = short > 0
        stats["dividend_years_penalized"] = int(bad.sum())
        penalty = penalty + short * RISK_PENALTY_DIVIDEND_YEARS

    if not skip.get("payout") and "payout_ratio_pct" in df.columns:
        low = (MIN_PAYOUT_RATIO_PCT - df["payout_ratio_pct"]).clip(lower=0)
        high = (df["payout_ratio_pct"] - MAX_PAYOUT_RATIO_PCT).clip(lower=0)
        bad = df["payout_ratio_pct"].notna() & ((low > 0) | (high > 0))
        stats["payout_penalized"] = int(bad.sum())
        penalty = penalty + bad.astype(float) * RISK_PENALTY_PAYOUT + (low + high) * 0.02

    if not skip.get("debt") and "debt_ratio_pct" in df.columns:
        if DEBT_RATIO_INDUSTRY_NEUTRAL:
            ind_mean = df.groupby(industry)["debt_ratio_pct"].transform("mean")
            cap = ind_mean * (1 + DEBT_RATIO_INDUSTRY_MARGIN_PCT / 100.0)
            cap = cap.fillna(MAX_DEBT_RATIO_PCT)
        else:
            cap = MAX_DEBT_RATIO_PCT
        excess = (df["debt_ratio_pct"] - cap).clip(lower=0)
        bad = df["debt_ratio_pct"].notna() & (excess > 0)
        stats["debt_penalized"] = int(bad.sum())
        penalty = penalty + bad.astype(float) * (RISK_PENALTY_DEBT + excess * 0.05)

    if not skip.get("interest") and "interest_coverage" in df.columns:
        short = (MIN_INTEREST_COVERAGE - df["interest_coverage"]).clip(lower=0)
        bad = df["interest_coverage"].notna() & (short > 0)
        stats["interest_cov_penalized"] = int(bad.sum())
        penalty = penalty + bad.astype(float) * (RISK_PENALTY_INTEREST + short * 0.5)

    return penalty, stats


def risk_filter_mask(
    df: pd.DataFrame,
    *,
    strategy_params=None,
    skip: dict[str, bool] | None = None,
    hard: bool = False,
) -> tuple[pd.Series, dict[str, int]]:
    stats: dict[str, int] = {}
    skip = skip or {}
    if not RISK_FILTER_ENABLED or df.empty:
        return pd.Series(True, index=df.index), stats

    if SOFT_RISK_SCORING_ENABLED and not hard:
        penalties, pen_stats = risk_score_penalties(df, skip=skip)
        stats.update(pen_stats)
        stats["risk_soft_mode"] = 1
        stats["risk_penalized"] = int((penalties > 0).sum())
        return pd.Series(True, index=df.index), stats

    ok = pd.Series(True, index=df.index)
    industry = _industry_col(df)

    if OCF_QUALITY_FILTER_ENABLED and not skip.get("ocf") and "ocf_to_profit" in df.columns:
        bad = df["ocf_to_profit"].notna() & (df["ocf_to_profit"] < MIN_OCF_TO_PROFIT)
        stats["ocf_excluded"] = int(bad.sum())
        ok &= ~bad

    if not skip.get("roe_vol") and "roe_volatility_ratio" in df.columns:
        if ROE_VOL_INDUSTRY_NEUTRAL:
            ind_mean = df.groupby(industry)["roe_volatility_ratio"].transform("mean")
            bad = df["roe_volatility_ratio"].notna() & ind_mean.notna() & (
                df["roe_volatility_ratio"] >= ind_mean
            )
        else:
            bad = df["roe_volatility_ratio"].notna() & (
                df["roe_volatility_ratio"] > MAX_ROE_VOLATILITY_RATIO
            )
        stats["roe_vol_excluded"] = int(bad.sum())
        ok &= ~bad

    if not skip.get("dividend_years") and "dividend_years_5y" in df.columns:
        bad = df["dividend_years_5y"] < MIN_DIVIDEND_YEARS
        stats["dividend_years_excluded"] = int(bad.sum())
        ok &= ~bad

    if not skip.get("payout") and "payout_ratio_pct" in df.columns:
        bad = df["payout_ratio_pct"].notna() & (
            (df["payout_ratio_pct"] < MIN_PAYOUT_RATIO_PCT)
            | (df["payout_ratio_pct"] > MAX_PAYOUT_RATIO_PCT)
        )
        stats["payout_excluded"] = int(bad.sum())
        ok &= ~bad

    if not skip.get("debt") and "debt_ratio_pct" in df.columns:
        if DEBT_RATIO_INDUSTRY_NEUTRAL:
            ind_mean = df.groupby(industry)["debt_ratio_pct"].transform("mean")
            cap = ind_mean * (1 + DEBT_RATIO_INDUSTRY_MARGIN_PCT / 100.0)
            cap = cap.fillna(MAX_DEBT_RATIO_PCT)
            bad = df["debt_ratio_pct"].notna() & (df["debt_ratio_pct"] > cap)
        else:
            bad = df["debt_ratio_pct"].notna() & (df["debt_ratio_pct"] > MAX_DEBT_RATIO_PCT)
        stats["debt_excluded"] = int(bad.sum())
        ok &= ~bad

    if not skip.get("interest") and "interest_coverage" in df.columns:
        bad = df["interest_coverage"].notna() & (df["interest_coverage"] < MIN_INTEREST_COVERAGE)
        stats["interest_cov_excluded"] = int(bad.sum())
        ok &= ~bad

    return ok, stats


def risk_pass_rate_by_industry(df: pd.DataFrame) -> pd.DataFrame:
    """各行业排雷通过率（仅排雷因子，不含股息/波动等前置筛选）。"""
    if df.empty or "industry" not in df.columns:
        return pd.DataFrame()
    industry = _industry_col(df)
    work = df.copy()
    work["industry"] = industry
    ok, _ = risk_filter_mask(work)
    rows: list[dict] = []
    for ind, sub in work.groupby("industry", observed=True):
        n = len(sub)
        passed = int(ok.loc[sub.index].sum())
        rows.append(
            {
                "industry": ind,
                "total": n,
                "passed": passed,
                "pass_rate_pct": round(passed / n * 100, 1) if n else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values(["pass_rate_pct", "total"], ascending=[True, False])


def check_risk_exclusion_timeline(
    code: str,
    records: pd.DataFrame,
    risk_hist: pd.DataFrame,
    *,
    start: str,
    end: str,
    event_date: str | None = None,
) -> pd.DataFrame:
    """检查单只股票在区间内何时被排雷剔除（用于暴雷股未来信息验证）。"""
    code = normalize_stock_code(code)
    div_index = build_dividend_year_index(records)
    rows: list[dict[str, Any]] = []
    dates = pd.date_range(start, end, freq="QS")
    if len(dates) == 0:
        dates = pd.DatetimeIndex([pd.Timestamp(start)])
    for as_of in dates:
        panel_row = {
            "code": code,
            "name": code,
            "price": 10.0,
            "cash_per_share": 0.5,
            "dividend_yield_pct": 5.0,
            "ann_vol_pct": 20.0,
            "eps": 1.0,
            "bps": 5.0,
            "ex_date": as_of - pd.Timedelta(days=30),
        }
        panel = pd.DataFrame([panel_row])
        panel = attach_risk_from_records(panel, records, as_of, div_index=div_index)
        panel = merge_risk_history(panel, risk_hist, as_of)
        mask, _ = risk_filter_mask(panel, hard=True)
        rows.append(
            {
                "as_of": as_of.date().isoformat(),
                "passed_risk": bool(mask.iloc[0]),
                "event_date": event_date,
            }
        )
    return pd.DataFrame(rows)
