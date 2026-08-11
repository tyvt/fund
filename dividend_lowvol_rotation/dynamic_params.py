"""动态阈值与因子权重（国债收益率 + 市场波动率）。"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from dividend_lowvol_rotation.config import (
    BOND_YIELD_REF_PCT,
    DYNAMIC_THRESHOLD_ENABLED,
    DYNAMIC_VOL_ENABLED,
    DYNAMIC_WEIGHT_ENABLED,
    MARKET_VOL_MEDIAN_MULT,
    MARKET_VOL_REF_PCT,
    MAX_ANNUALIZED_VOL_PCT,
    MAX_VOL_CEILING_PCT,
    MIN_DIVIDEND_YIELD_FLOOR_PCT,
    MIN_DIVIDEND_YIELD_PCT,
    MIN_VOL_FLOOR_PCT,
    MIN_YIELD_SPREAD_OVER_BOND_PCT,
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


def _fetch_bond_yield_pct() -> tuple[float | None, str | None]:
    try:
        from market_data import get_gov_bond_yield

        payload = get_gov_bond_yield()
        return float(payload["bond_yield"]) * 100.0, payload.get("data_date")
    except Exception:
        return None, None


def resolve_dynamic_params(
    panel: pd.DataFrame | None = None,
    *,
    strategy_params: StrategyParams | None = None,
) -> DynamicParams:
    sp = strategy_params or StrategyParams()
    notes: list[str] = []
    bond_pct, bond_date = _fetch_bond_yield_pct()

    min_yield = sp.min_dividend_yield_pct if sp.min_dividend_yield_pct is not None else MIN_DIVIDEND_YIELD_PCT
    dyn_threshold = (
        DYNAMIC_THRESHOLD_ENABLED if sp.dynamic_threshold_enabled is None else sp.dynamic_threshold_enabled
    )
    spread = (
        sp.min_yield_spread_over_bond_pct
        if sp.min_yield_spread_over_bond_pct is not None
        else MIN_YIELD_SPREAD_OVER_BOND_PCT
    )
    if dyn_threshold and bond_pct is not None:
        dyn_yield = max(MIN_DIVIDEND_YIELD_FLOOR_PCT, bond_pct + spread)
        min_yield = dyn_yield if sp.min_dividend_yield_pct is None else max(min_yield, dyn_yield)
        notes.append(
            f"动态股息率门槛 {dyn_yield:.2f}%（国债 {bond_pct:.2f}%"
            f"{f'，{bond_date}' if bond_date else ''} + 利差 {spread:.2f}%）"
        )
    else:
        notes.append(f"股息率门槛 {min_yield:.2f}%")

    max_vol = sp.max_annualized_vol_pct if sp.max_annualized_vol_pct is not None else MAX_ANNUALIZED_VOL_PCT
    market_median = None
    if panel is not None and not panel.empty and "ann_vol_pct" in panel.columns:
        market_median = float(panel["ann_vol_pct"].median())
    vol_mult = sp.market_vol_median_mult if sp.market_vol_median_mult is not None else MARKET_VOL_MEDIAN_MULT
    dyn_vol = DYNAMIC_VOL_ENABLED if sp.dynamic_vol_enabled is None else sp.dynamic_vol_enabled
    if dyn_vol and market_median is not None:
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
