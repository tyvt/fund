"""动态阈值与因子权重（国债收益率 + 市场波动率）。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import pandas as pd

from dividend_lowvol_rotation.config import (
    BOND_YIELD_REF_PCT,
    DYNAMIC_VOL_ENABLED,
    DYNAMIC_WEIGHT_ENABLED,
    INDEX_ANNUAL_REBALANCE_TIMING,
    MARKET_VOL_MEDIAN_MULT,
    MARKET_VOL_REF_PCT,
    MAX_ANNUALIZED_VOL_PCT,
    MAX_VOL_CEILING_PCT,
    MIN_DIVIDEND_YIELD_FLOOR_PCT,
    MIN_DIVIDEND_YIELD_PCT,
    MIN_VOL_FLOOR_PCT,
    VOL_RANK_WEIGHT,
    VOL_WEIGHT_BASE,
    VOL_WEIGHT_MARKET_SENS,
    YIELD_RANK_WEIGHT,
    YIELD_WEIGHT_BASE,
    YIELD_WEIGHT_BOND_SENS,
)
from dividend_lowvol_rotation.strategy_params import StrategyParams


@dataclass(frozen=True)
class DynamicParams:
    min_dividend_yield_pct: float
    max_annualized_vol_pct: float
    yield_rank_weight: float
    vol_rank_weight: float
    bond_yield_pct: float | None
    market_vol_median_pct: float | None
    notes: list[str]


@lru_cache(maxsize=1)
def _bond_yield_history_series() -> pd.Series:
    """本地缓存的 10 年期国债收益率（小数 → 百分数）。"""
    try:
        from data_cache import cache_path, load_dataframe

        path = cache_path("bond_yield_history", subdir="cn")
        df = load_dataframe(path, parse_dates=["date"])
        if df is None or df.empty:
            return pd.Series(dtype=float)
        date_col = "date" if "date" in df.columns else "SOLAR_DATE"
        ycol = "bond_yield" if "bond_yield" in df.columns else None
        if ycol is None:
            return pd.Series(dtype=float)
        out = df[[date_col, ycol]].dropna()
        out[date_col] = pd.to_datetime(out[date_col])
        s = out.set_index(date_col)[ycol].astype(float) * 100.0
        return s.sort_index()
    except Exception:
        return pd.Series(dtype=float)


def bond_yield_pct_as_of(as_of: pd.Timestamp | None = None) -> float | None:
    """回测/历史截面用：取 as_of 当日或之前最近一期国债收益率（%）。"""
    hist = _bond_yield_history_series()
    if not hist.empty:
        ts = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.today()
        sub = hist[hist.index <= ts]
        if not sub.empty:
            return float(sub.iloc[-1])
    pct, _ = _fetch_bond_yield_pct()
    return pct


@lru_cache(maxsize=1)
def _fetch_bond_yield_pct() -> tuple[float | None, str | None]:
    try:
        from market_data import get_gov_bond_yield

        result = get_gov_bond_yield()
        if result is None:
            return None, None
        if isinstance(result, tuple):
            y, d = result
            if y is None:
                return None, None
            date_s = d.isoformat() if hasattr(d, "isoformat") else str(d)
            return float(y) * 100.0, date_s
        return float(result["bond_yield"]) * 100.0, result.get("data_date")
    except Exception:
        return None, None


def resolve_dynamic_params(
    panel: pd.DataFrame | None = None,
    *,
    as_of: pd.Timestamp | None = None,
    strategy_params: StrategyParams | None = None,
) -> DynamicParams:
    sp = strategy_params or StrategyParams()
    notes: list[str] = []
    bond_pct = bond_yield_pct_as_of(as_of) if as_of is not None else _fetch_bond_yield_pct()[0]
    bond_date = as_of.date().isoformat() if as_of is not None else _fetch_bond_yield_pct()[1]

    min_yield = sp.min_dividend_yield_pct if sp.min_dividend_yield_pct is not None else MIN_DIVIDEND_YIELD_PCT
    if INDEX_ANNUAL_REBALANCE_TIMING == "january":
        min_yield = (
            MIN_DIVIDEND_YIELD_FLOOR_PCT
            if sp.min_dividend_yield_pct is None
            else max(min_yield, MIN_DIVIDEND_YIELD_FLOOR_PCT)
        )
        notes.append(f"1月调仓股息率底线 {min_yield:.2f}%")
    else:
        notes.append(f"股息率门槛 {min_yield:.2f}%")

    max_vol = sp.max_annualized_vol_pct if sp.max_annualized_vol_pct is not None else MAX_ANNUALIZED_VOL_PCT
    market_median = None
    if panel is not None and not panel.empty and "ann_vol_pct" in panel.columns:
        market_median = float(panel["ann_vol_pct"].median())
    vol_mult = sp.market_vol_median_mult if sp.market_vol_median_mult is not None else MARKET_VOL_MEDIAN_MULT
    dyn_vol = DYNAMIC_VOL_ENABLED if sp.dynamic_vol_enabled is None else sp.dynamic_vol_enabled
    # 1 月调仓候选池波动偏高，勿用 40% 顶板压死池子
    if INDEX_ANNUAL_REBALANCE_TIMING == "january":
        max_vol = (
            MAX_ANNUALIZED_VOL_PCT
            if sp.max_annualized_vol_pct is None
            else max(max_vol, MAX_ANNUALIZED_VOL_PCT)
        )
        notes.append(f"1月调仓波动上限 {max_vol:.1f}%")
    elif dyn_vol and market_median is not None:
        computed = min(
            MAX_VOL_CEILING_PCT,
            max(MIN_VOL_FLOOR_PCT, market_median * vol_mult),
        )
        max_vol = computed if sp.max_annualized_vol_pct is None else min(max_vol, computed)
        notes.append(
            f"动态波动上限 {computed:.1f}%（候选池中位 {market_median:.1f}% × {vol_mult:g}）"
        )
    else:
        notes.append(f"波动上限 {max_vol:.1f}%")

    yield_w = sp.yield_rank_weight if sp.yield_rank_weight is not None else YIELD_RANK_WEIGHT
    vol_w = sp.vol_rank_weight if sp.vol_rank_weight is not None else VOL_RANK_WEIGHT
    dyn_weight = DYNAMIC_WEIGHT_ENABLED if sp.dynamic_weight_enabled is None else sp.dynamic_weight_enabled
    if dyn_weight:
        if bond_pct is not None and sp.yield_rank_weight is None:
            yield_w = max(0.5, YIELD_WEIGHT_BASE + YIELD_WEIGHT_BOND_SENS * (bond_pct - BOND_YIELD_REF_PCT))
        if market_median is not None and sp.vol_rank_weight is None:
            vol_w = max(0.25, VOL_WEIGHT_BASE + VOL_WEIGHT_MARKET_SENS * (market_median - MARKET_VOL_REF_PCT))
        notes.append(f"动态权重：股息率 {yield_w:.2f}，低波 {vol_w:.2f}")
    else:
        notes.append(f"权重：股息率 {yield_w:g}，低波 {vol_w:g}")

    return DynamicParams(
        min_dividend_yield_pct=min_yield,
        max_annualized_vol_pct=max_vol,
        yield_rank_weight=yield_w,
        vol_rank_weight=vol_w,
        bond_yield_pct=bond_pct,
        market_vol_median_pct=market_median,
        notes=notes,
    )
