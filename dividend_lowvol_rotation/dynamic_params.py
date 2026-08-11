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
    VOL_WEIGHT_BASE,
    VOL_WEIGHT_MARKET_SENS,
    YIELD_RANK_WEIGHT,
    YIELD_WEIGHT_BASE,
    YIELD_WEIGHT_BOND_SENS,
    VOL_RANK_WEIGHT,
)


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


def resolve_dynamic_params(panel: pd.DataFrame | None = None) -> DynamicParams:
    notes: list[str] = []
    bond_pct, bond_date = _fetch_bond_yield_pct()

    min_yield = MIN_DIVIDEND_YIELD_PCT
    if DYNAMIC_THRESHOLD_ENABLED and bond_pct is not None:
        dyn_yield = max(MIN_DIVIDEND_YIELD_FLOOR_PCT, bond_pct + MIN_YIELD_SPREAD_OVER_BOND_PCT)
        min_yield = dyn_yield
        notes.append(
            f"动态股息率门槛 {dyn_yield:.2f}%（国债 {bond_pct:.2f}%"
            f"{f'，{bond_date}' if bond_date else ''} + 利差 {MIN_YIELD_SPREAD_OVER_BOND_PCT:.2f}%）"
        )
    else:
        notes.append(f"静态股息率门槛 {min_yield:.2f}%")

    max_vol = MAX_ANNUALIZED_VOL_PCT
    market_median = None
    if panel is not None and not panel.empty and "ann_vol_pct" in panel.columns:
        market_median = float(panel["ann_vol_pct"].median())
    if DYNAMIC_VOL_ENABLED and market_median is not None:
        dyn_vol = min(
            MAX_VOL_CEILING_PCT,
            max(MIN_VOL_FLOOR_PCT, market_median * MARKET_VOL_MEDIAN_MULT),
        )
        max_vol = dyn_vol
        notes.append(
            f"动态波动上限 {dyn_vol:.1f}%（候选池中位 {market_median:.1f}% × {MARKET_VOL_MEDIAN_MULT:g}）"
        )
    else:
        notes.append(f"静态波动上限 {max_vol:.1f}%")

    yield_w = YIELD_RANK_WEIGHT
    vol_w = VOL_RANK_WEIGHT
    if DYNAMIC_WEIGHT_ENABLED:
        if bond_pct is not None:
            yield_w = max(0.5, YIELD_WEIGHT_BASE + YIELD_WEIGHT_BOND_SENS * (bond_pct - BOND_YIELD_REF_PCT))
        if market_median is not None:
            vol_w = max(0.25, VOL_WEIGHT_BASE + VOL_WEIGHT_MARKET_SENS * (market_median - MARKET_VOL_REF_PCT))
        notes.append(f"动态权重：股息率 {yield_w:.2f}，低波 {vol_w:.2f}")
    else:
        notes.append(f"静态权重：股息率 {yield_w:g}，低波 {vol_w:g}")

    return DynamicParams(
        min_dividend_yield_pct=min_yield,
        max_annualized_vol_pct=max_vol,
        yield_rank_weight=yield_w,
        vol_rank_weight=vol_w,
        bond_yield_pct=bond_pct,
        market_vol_median_pct=market_median,
        notes=notes,
    )
