"""Reusable, side-effect-free cross-sectional signal functions."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd


def _aligned_mask(frame: pd.DataFrame, mask: pd.DataFrame | None) -> pd.DataFrame:
    if mask is None:
        return pd.DataFrame(True, index=frame.index, columns=frame.columns)
    return mask.reindex(index=frame.index, columns=frame.columns).fillna(False).astype(bool)


def rank_by_factor(
    factor: pd.DataFrame, *, ascending: bool = False, mask: pd.DataFrame | None = None
) -> pd.DataFrame:
    eligible = factor.where(_aligned_mask(factor, mask))
    return eligible.rank(axis=1, ascending=ascending, method="first", na_option="bottom")


def filter_by_threshold(
    factor: pd.DataFrame, threshold: float, *, operator: str = ">="
) -> pd.DataFrame:
    operations = {
        ">=": factor.ge,
        ">": factor.gt,
        "<=": factor.le,
        "<": factor.lt,
        "==": factor.eq,
    }
    if operator not in operations:
        raise ValueError(f"不支持的比较符：{operator}")
    return operations[operator](threshold) & factor.notna()


def filter_by_quantile(
    factor: pd.DataFrame, quantile: float, *, upper: bool = True
) -> pd.DataFrame:
    boundary = factor.quantile(quantile, axis=1)
    return factor.ge(boundary, axis=0) if upper else factor.le(boundary, axis=0)


def compute_equal_weight(
    selected: pd.DataFrame, *, max_weight: float | None = None
) -> pd.DataFrame:
    selected = selected.fillna(False).astype(bool)
    counts = selected.sum(axis=1).replace(0, np.nan)
    weights = selected.div(counts, axis=0).fillna(0.0)
    return _cap_and_normalize(weights, max_weight)


def compute_weight_by_factor(
    factor: pd.DataFrame,
    *,
    selected: pd.DataFrame,
    max_weight: float | None = None,
) -> pd.DataFrame:
    raw = factor.where(selected).clip(lower=0).fillna(0.0)
    totals = raw.sum(axis=1).replace(0, np.nan)
    weights = raw.div(totals, axis=0).fillna(0.0)
    fallback = compute_equal_weight(selected, max_weight=None)
    weights = weights.where(weights.sum(axis=1).gt(0), fallback)
    return _cap_and_normalize(weights, max_weight)


def _cap_and_normalize(weights: pd.DataFrame, cap: float | None) -> pd.DataFrame:
    if cap is None or cap <= 0:
        return weights
    out = weights.copy()
    eligible = out.gt(0.0)
    for _ in range(30):
        over = out.gt(cap) & eligible
        if not over.to_numpy().any():
            break
        capped = out.clip(upper=cap)
        remaining = (1.0 - capped.sum(axis=1)).clip(lower=0.0)
        room = (cap - capped).clip(lower=0.0).where(eligible, 0.0)
        room_total = room.sum(axis=1).replace(0, np.nan)
        out = capped + room.div(room_total, axis=0).mul(remaining, axis=0).fillna(0.0)
    # 当 cap × 入选数 < 100% 时，剩余部分就是现金，不能再次归一化而突破上限。
    return out.where(eligible, 0.0)


def apply_industry_cap(
    weights: pd.DataFrame,
    industries: Mapping[str, str] | pd.Series,
    *,
    max_weight: float,
) -> pd.DataFrame:
    mapping = dict(industries)
    out = weights.copy()
    if max_weight <= 0:
        return out
    grouped: dict[str, list[str]] = {}
    for column in out.columns:
        grouped.setdefault(str(mapping.get(str(column), "未分类")), []).append(column)
    for columns in grouped.values():
        totals = out.loc[:, columns].sum(axis=1)
        scale = (float(max_weight) / totals.replace(0, np.nan)).clip(upper=1.0).fillna(1.0)
        out.loc[:, columns] = out.loc[:, columns].mul(scale, axis=0)
    return out


def apply_beta_constraint(
    weights: pd.DataFrame,
    beta: pd.DataFrame,
    *,
    beta_min: float | None = None,
    beta_max: float | None = None,
) -> pd.DataFrame:
    mask = beta.notna()
    if beta_min is not None:
        mask &= beta.ge(beta_min)
    if beta_max is not None:
        mask &= beta.le(beta_max)
    return weights.where(mask, 0.0)


def apply_market_cap_cap(
    weights: pd.DataFrame,
    total_mv: pd.DataFrame,
    *,
    small_cap_quantile: float = 0.3,
    small_cap_weight_max: float = 0.4,
) -> pd.DataFrame:
    threshold = total_mv.quantile(small_cap_quantile, axis=1)
    small = total_mv.le(threshold, axis=0)
    out = weights.copy()
    if small_cap_weight_max <= 0:
        return out.where(~small, 0.0)
    small_total = out.where(small, 0.0).sum(axis=1)
    scale = (float(small_cap_weight_max) / small_total.replace(0, np.nan)).clip(upper=1.0).fillna(1.0)
    return out.where(~small, out.mul(scale, axis=0))
