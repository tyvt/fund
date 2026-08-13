# -*- coding: utf-8 -*-
"""前置风控：波动预警、市场宽度、组合仓位缩放。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from dividend_lowvol_rotation.config import (
    BREADTH_BELOW_MA250_SCALE,
    BREADTH_BELOW_MA250_THRESHOLD_PCT,
    MARKET_BREADTH_ENABLED,
    MARKET_REGIME_ENABLED,
    VIX_PROXY_POSITION_TIERS,
    VOL_TARGET_ENABLED,
    VOL_TARGET_PCT,
)


def market_breadth_below_ma250_pct(panel: pd.DataFrame) -> float | None:
    """候选池中跌破 MA250 的标的占比（0~100）。"""
    if panel.empty or "ma_250" not in panel.columns:
        return None
    price = pd.to_numeric(panel.get("price"), errors="coerce")
    ma = pd.to_numeric(panel["ma_250"], errors="coerce")
    valid = price.notna() & ma.notna() & (ma > 0)
    if not valid.any():
        return None
    below = (price[valid] < ma[valid]).sum()
    return float(below / valid.sum() * 100)


def resolve_position_scale(
    *,
    market_vol_median_pct: float | None,
    panel: pd.DataFrame | None = None,
    portfolio_vol_pct: float | None = None,
) -> tuple[float, list[str]]:
    """综合波动预警、市场宽度、波动率目标，返回权益仓位比例 (0~1]。"""
    scales: list[float] = [1.0]
    notes: list[str] = []

    if MARKET_REGIME_ENABLED and market_vol_median_pct is not None:
        vix = float(market_vol_median_pct)
        for threshold, scale in VIX_PROXY_POSITION_TIERS:
            if vix >= threshold:
                scales.append(scale)
                notes.append(f"波动预警 {vix:.1f}%≥{threshold:g}% → 仓位 {scale * 100:.0f}%")
                break

    if MARKET_BREADTH_ENABLED and panel is not None:
        breadth = market_breadth_below_ma250_pct(panel)
        if breadth is not None and breadth >= BREADTH_BELOW_MA250_THRESHOLD_PCT:
            scales.append(BREADTH_BELOW_MA250_SCALE)
            notes.append(
                f"市场宽度：{breadth:.1f}% 跌破 MA250（≥{BREADTH_BELOW_MA250_THRESHOLD_PCT:g}%）"
                f" → 仓位 {BREADTH_BELOW_MA250_SCALE * 100:.0f}%"
            )

    if VOL_TARGET_ENABLED and portfolio_vol_pct is not None and portfolio_vol_pct > 0:
        if portfolio_vol_pct > VOL_TARGET_PCT:
            vol_scale = VOL_TARGET_PCT / portfolio_vol_pct
            scales.append(vol_scale)
            notes.append(
                f"波动率目标：组合 {portfolio_vol_pct:.1f}% > 目标 {VOL_TARGET_PCT:g}%"
                f" → 仓位 {vol_scale * 100:.0f}%"
            )

    scale = float(min(scales))
    return max(0.0, min(1.0, scale)), notes


def estimate_portfolio_vol_pct(
    lots: dict,
    store,
    as_of: pd.Timestamp,
    panel: pd.DataFrame,
) -> float | None:
    """持仓加权预估年化波动率。"""
    if not lots:
        return None
    vols: list[float] = []
    weights: list[float] = []
    for code, lot in lots.items():
        metrics = store.metrics_at(code, as_of)
        vol = metrics.get("ann_vol_pct")
        price = metrics.get("price") or store.price_at(code, as_of)
        if vol is None and not panel.empty and "ann_vol_pct" in panel.columns:
            row = panel[panel["code"] == code]
            if not row.empty:
                vol = float(row["ann_vol_pct"].iloc[0])
        if price and price > 0 and vol is not None:
            mv = lot.shares * price
            vols.append(float(vol))
            weights.append(mv)
    if not vols:
        return None
    w = np.array(weights, dtype=float)
    if w.sum() <= 0:
        return float(np.mean(vols))
    return float(np.average(vols, weights=w))


def resolve_stop_loss_pct(market_vol_median_pct: float | None) -> float:
    from dividend_lowvol_rotation.config import (
        STOP_LOSS_HIGH_VOL_PCT,
        STOP_LOSS_LOW_VOL_PCT,
        STOP_LOSS_VOL_THRESHOLD_PCT,
    )

    if market_vol_median_pct is not None and market_vol_median_pct >= STOP_LOSS_VOL_THRESHOLD_PCT:
        return STOP_LOSS_HIGH_VOL_PCT
    return STOP_LOSS_LOW_VOL_PCT


def resolve_grace_period_days(market_vol_median_pct: float | None) -> int:
    from dividend_lowvol_rotation.config import (
        GRACE_VOL_TIER_ADJUSTMENTS,
        SELL_GRACE_PERIOD_DAYS,
    )

    base = SELL_GRACE_PERIOD_DAYS
    if market_vol_median_pct is None:
        return base
    adj = 0
    for threshold, days_add in GRACE_VOL_TIER_ADJUSTMENTS:
        if market_vol_median_pct >= threshold:
            adj = days_add
    return base + adj
