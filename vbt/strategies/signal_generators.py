"""Reusable, side-effect-free cross-sectional signal functions."""

from __future__ import annotations

import math
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


def compute_fusion_score(
    df: pd.DataFrame | Mapping[str, pd.DataFrame],
    volatility: pd.DataFrame | Mapping[str, float] | None = None,
    factors: list[str] | tuple[str, ...] | None = None,
    *,
    weights: Mapping[str, float] | None = None,
    directions: Mapping[str, int] | None = None,
    min_valid_factors: int | None = None,
    dividend_weight: float = 0.5,
    volatility_weight: float = 0.5,
    mask: pd.DataFrame | None = None,
) -> pd.Series | pd.DataFrame:
    """Return a direction-adjusted cross-sectional fusion score.

    The generic API is ``compute_fusion_score(data, weights, factors)`` where
    ``data`` is either a factor-to-matrix mapping or a long-form DataFrame.
    The former two-factor dividend/volatility API remains supported.  Ranking
    is always within a date's cross-section, so future observations cannot
    leak into the score.
    """
    if isinstance(volatility, Mapping):
        if weights is not None:
            raise ValueError("weights was supplied both positionally and by keyword")
        weights = {str(key): float(value) for key, value in volatility.items()}
        volatility = None

    if weights is not None or isinstance(df, Mapping) or factors is not None:
        selected_factors = list(factors or (weights or {}).keys())
        if not selected_factors:
            raise ValueError("multi-factor fusion requires at least one factor")
        raw_weights = dict(weights or {name: 1.0 for name in selected_factors})
        missing_weights = set(selected_factors) - set(raw_weights)
        if missing_weights:
            raise KeyError(f"fusion weights missing: {', '.join(sorted(missing_weights))}")
        selected_weights = {name: float(raw_weights[name]) for name in selected_factors}
        if any(value < 0.0 for value in selected_weights.values()):
            raise ValueError("fusion weights must be non-negative")
        weight_total = sum(selected_weights.values())
        if weight_total <= 0.0:
            raise ValueError("fusion weights must sum to a positive value")
        selected_weights = {
            name: value / weight_total for name, value in selected_weights.items()
        }
        factor_directions = {name: 1 for name in selected_factors}
        factor_directions.update({str(k): int(v) for k, v in (directions or {}).items()})
        required_valid = (
            max(1, int(min_valid_factors))
            if min_valid_factors is not None
            else len(selected_factors)
        )
        required_valid = min(required_valid, len(selected_factors))

        if isinstance(df, Mapping):
            absent = set(selected_factors) - set(df)
            if absent:
                raise KeyError(f"fusion data missing: {', '.join(sorted(absent))}")
            base = df[selected_factors[0]].astype(float)
            hard_mask = _aligned_mask(base, mask)
            score = pd.DataFrame(0.0, index=base.index, columns=base.columns)
            valid_count = pd.DataFrame(0, index=base.index, columns=base.columns)
            for name in selected_factors:
                values = df[name].reindex_like(base).astype(float)
                valid = values.notna() & hard_mask
                direction = 1 if factor_directions.get(name, 1) >= 0 else -1
                ranks = (values * direction).where(valid).rank(
                    axis=1, pct=True, method="average"
                )
                score += selected_weights[name] * ranks.fillna(0.5)
                valid_count += valid.astype(int)
            return score.where(hard_mask & valid_count.ge(required_valid))

        absent = set(selected_factors) - set(df.columns)
        if absent:
            raise KeyError(f"fusion data missing: {', '.join(sorted(absent))}")
        group_key = "trade_date" if "trade_date" in df.columns else None
        score = pd.Series(0.0, index=df.index, dtype=float)
        valid_count = pd.Series(0, index=df.index, dtype=int)
        for name in selected_factors:
            values = pd.to_numeric(df[name], errors="coerce")
            direction = 1 if factor_directions.get(name, 1) >= 0 else -1
            ranked_input = values * direction
            ranks = (
                ranked_input.groupby(df[group_key]).rank(pct=True, method="average")
                if group_key is not None
                else ranked_input.rank(pct=True, method="average")
            )
            score += selected_weights[name] * ranks.fillna(0.5)
            valid_count += values.notna().astype(int)
        return score.where(valid_count.ge(required_valid))

    div_weight = float(dividend_weight)
    vol_weight = float(volatility_weight)
    if div_weight < 0.0 or vol_weight < 0.0 or div_weight + vol_weight <= 0.0:
        raise ValueError("融合排序权重必须非负且合计大于 0")
    total = div_weight + vol_weight
    div_weight /= total
    vol_weight /= total

    if volatility is None:
        required = {"dividend_yield", "volatility_60d"}
        missing = required - set(df.columns)
        if missing:
            raise KeyError(f"融合评分缺少字段：{', '.join(sorted(missing))}")
        dividend = pd.to_numeric(df["dividend_yield"], errors="coerce")
        vol = pd.to_numeric(df["volatility_60d"], errors="coerce")
        valid = dividend.notna() & vol.notna()
        div_rank = dividend.where(valid).rank(pct=True, method="average")
        vol_rank = vol.where(valid).rank(pct=True, method="average")
        return (div_weight * div_rank + vol_weight * (1.0 - vol_rank)).where(valid)

    dividend = df.astype(float)
    vol = volatility.reindex_like(dividend).astype(float)
    valid = dividend.notna() & vol.notna() & _aligned_mask(dividend, mask)
    div_rank = dividend.where(valid).rank(axis=1, pct=True, method="average")
    vol_rank = vol.where(valid).rank(axis=1, pct=True, method="average")
    return (div_weight * div_rank + vol_weight * (1.0 - vol_rank)).where(valid)


def select_by_fusion_score(
    df: pd.DataFrame | pd.Series,
    score_col: str = "fusion_score",
    *,
    top_n: int = 10,
) -> pd.DataFrame | pd.Series:
    """Select the highest fusion scores from one or many cross-sections."""
    limit = int(top_n)
    if limit <= 0:
        raise ValueError("融合排序 Top N 必须为正整数")
    if isinstance(df, pd.Series):
        chosen = df.dropna().nlargest(limit).index
        return pd.Series(df.index.isin(chosen), index=df.index)
    if score_col in df.columns:
        return df.nlargest(limit, score_col)
    ranks = df.rank(axis=1, ascending=False, method="first", na_option="bottom")
    return ranks.le(limit) & df.notna()


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


def filter_volatility_top(
    df: pd.DataFrame | pd.Series,
    vol_col: str = "volatility_60d",
    *,
    top_n: int = 10,
) -> pd.DataFrame | pd.Series:
    """Select the ``top_n`` lowest-volatility names in each cross-section.

    A date-by-security matrix is ranked across columns for every date.  A
    Series, or ``vol_col`` from a long-form frame, is treated as one
    cross-section.  ``method='first'`` makes ties deterministic and keeps the
    selection at no more than ``top_n`` names.
    """
    limit = int(top_n)
    if limit <= 0:
        raise ValueError("波动率 Top N 必须为正整数")

    is_long_form = isinstance(df, pd.DataFrame) and vol_col in df.columns
    factor = df[vol_col] if is_long_form else df
    if isinstance(factor, pd.Series):
        selected = factor.dropna().nsmallest(limit)
        return pd.Series(factor.index.isin(selected.index), index=factor.index)

    ranks = factor.rank(
        axis=1,
        ascending=True,
        method="first",
        na_option="bottom",
    )
    return ranks.le(limit) & factor.notna()


def filter_volatility_band(
    df: pd.DataFrame | pd.Series,
    vol_col: str = "volatility_60d",
    *,
    lower_quantile: float = 0.20,
    upper_quantile: float = 0.80,
) -> pd.DataFrame | pd.Series:
    """Keep the middle cross-sectional volatility band (Q2-Q4 by default).

    Matrix input is interpreted as dates by securities and is evaluated across
    columns on every date.  A Series, or ``vol_col`` from a long-form frame, is
    evaluated as one cross-section.  Boundaries are deliberately strict so the
    lowest and highest quintiles are both excluded.
    """
    lower = float(lower_quantile)
    upper = float(upper_quantile)
    if not 0.0 <= lower < upper <= 1.0:
        raise ValueError("波动率 Band 分位点必须满足 0 <= lower < upper <= 1")

    is_long_form = isinstance(df, pd.DataFrame) and vol_col in df.columns
    factor = df[vol_col] if is_long_form else df
    if isinstance(factor, pd.Series):
        lower_boundary = factor.quantile(lower)
        upper_boundary = factor.quantile(upper)
        return factor.gt(lower_boundary) & factor.lt(upper_boundary) & factor.notna()

    lower_boundary = factor.quantile(lower, axis=1)
    upper_boundary = factor.quantile(upper, axis=1)
    return (
        factor.gt(lower_boundary, axis=0)
        & factor.lt(upper_boundary, axis=0)
        & factor.notna()
    )


def apply_percentile_filters(
    df: pd.DataFrame | pd.Series,
    factor_col: str | None = None,
    lower_pct: float | None = 0.20,
    upper_pct: float | None = 0.80,
) -> pd.DataFrame | pd.Series:
    """Return a cross-sectional percentile mask without using absolute cutoffs.

    A Series (or ``factor_col`` from a long-form DataFrame) is ranked over its
    index.  A matrix is ranked across columns for every date.  Either boundary
    can be disabled with ``None`` so ROE can use only a lower bound while debt
    ratio uses only an upper bound.
    """
    factor = df[factor_col] if factor_col is not None else df
    if lower_pct is not None and not 0.0 <= float(lower_pct) <= 1.0:
        raise ValueError("lower_pct 必须位于 [0, 1]")
    if upper_pct is not None and not 0.0 <= float(upper_pct) <= 1.0:
        raise ValueError("upper_pct 必须位于 [0, 1]")
    if lower_pct is not None and upper_pct is not None and lower_pct >= upper_pct:
        raise ValueError("lower_pct 必须小于 upper_pct")
    axis = 0 if isinstance(factor, pd.Series) else 1
    rank_pct = factor.rank(axis=axis, pct=True, method="average")
    mask = factor.notna()
    if lower_pct is not None:
        mask &= rank_pct.gt(float(lower_pct))
    if upper_pct is not None:
        mask &= rank_pct.le(float(upper_pct))
    return mask


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


def compute_constrained_weight_by_factor(
    factor: pd.DataFrame,
    *,
    selected: pd.DataFrame,
    max_weight: float | None = None,
    industries: Mapping[str, str] | pd.Series | None = None,
    industry_max: float | None = None,
    total_mv: pd.DataFrame | None = None,
    small_cap_quantile: float = 0.3,
    small_cap_weight_max: float | None = None,
    target_weight: float = 1.0,
) -> pd.DataFrame:
    """Allocate up to ``target_weight`` while preserving every active cap.

    Unlike post-allocation clipping, residual weight is repeatedly distributed
    to securities and groups that still have capacity.  If the selected set is
    infeasible, the returned row remains below the target so the caller can
    expand its candidate pool instead of silently creating cash or violating a
    limit.
    """
    chosen = _aligned_mask(factor, selected)
    out = pd.DataFrame(0.0, index=factor.index, columns=factor.columns)
    single_cap = float(max_weight) if max_weight is not None and max_weight > 0 else 1.0
    target = min(1.0, max(0.0, float(target_weight)))
    industry_cap = (
        float(industry_max) if industry_max is not None and industry_max > 0 else None
    )
    small_cap = (
        float(small_cap_weight_max)
        if small_cap_weight_max is not None and small_cap_weight_max > 0
        else None
    )
    industry_map = dict(industries) if industries is not None else {}

    for day in factor.index:
        eligible = chosen.loc[day].fillna(False).to_numpy(dtype=bool)
        if not eligible.any() or target <= 0:
            continue
        raw = factor.loc[day].to_numpy(dtype=float)
        positive = np.isfinite(raw) & (raw > 0) & eligible
        floor = float(np.nanmin(raw[positive])) * 0.01 if positive.any() else 1.0
        scores = np.where(positive, raw, np.where(eligible, floor, 0.0))
        allocation = np.zeros(len(factor.columns), dtype=float)
        column_industries = np.asarray(
            [str(industry_map.get(str(column), "未分类")) for column in factor.columns],
            dtype=object,
        )
        if total_mv is not None and day in total_mv.index:
            market_values = total_mv.reindex(columns=factor.columns).loc[day].to_numpy(dtype=float)
            finite_mv = market_values[np.isfinite(market_values)]
            threshold = (
                float(np.quantile(finite_mv, small_cap_quantile)) if finite_mv.size else np.nan
            )
            is_small = eligible & np.isfinite(market_values) & (market_values <= threshold)
        else:
            is_small = np.zeros(len(factor.columns), dtype=bool)

        for _ in range(500):
            remaining = target - float(allocation.sum())
            if remaining <= 1e-10:
                break
            capacity = np.where(eligible, np.maximum(single_cap - allocation, 0.0), 0.0)
            active = capacity > 1e-12
            if not active.any():
                break
            preference = np.where(active, scores, 0.0)
            if preference.sum() <= 0:
                preference = active.astype(float)
            increment = remaining * preference / preference.sum()
            increment = np.minimum(increment, capacity)

            if industry_cap is not None:
                for industry in np.unique(column_industries[active]):
                    members = eligible & (column_industries == industry)
                    room = max(industry_cap - float(allocation[members].sum()), 0.0)
                    proposed = float(increment[members].sum())
                    if proposed > room and proposed > 0:
                        increment[members] *= room / proposed
            if small_cap is not None and is_small.any():
                room = max(small_cap - float(allocation[is_small].sum()), 0.0)
                proposed = float(increment[is_small].sum())
                if proposed > room and proposed > 0:
                    increment[is_small] *= room / proposed

            added = float(increment.sum())
            if added <= 1e-12:
                break
            allocation += increment
        out.loc[day] = allocation
    return out


def select_stocks_with_fallback(
    factor: pd.DataFrame,
    *,
    primary_eligible: pd.DataFrame,
    relaxed_eligible: pd.DataFrame,
    hard_eligible: pd.DataFrame,
    top_n: int = 10,
    min_holdings: int = 10,
    fallback_top_n: int = 100,
    min_investment_pct: float = 1.0,
    max_single_weight: float | None = 0.08,
    industries: Mapping[str, str] | pd.Series | None = None,
    industry_max: float | None = None,
    total_mv: pd.DataFrame | None = None,
    small_cap_weight_max: float | None = None,
    max_holdings: int | None = None,
    allow_partial: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """Select, expand and allocate each cross-section until caps permit full investment."""
    factor = factor.astype(float)
    masks = [
        _aligned_mask(factor, primary_eligible),
        _aligned_mask(factor, relaxed_eligible),
        _aligned_mask(factor, hard_eligible),
    ]
    selected = pd.DataFrame(False, index=factor.index, columns=factor.columns)
    weights = pd.DataFrame(0.0, index=factor.index, columns=factor.columns)
    tiers = pd.Series("unfilled", index=factor.index, dtype=object)
    target = min(1.0, max(0.0, float(min_investment_pct)))
    cap_count = (
        int(math.ceil(target / float(max_single_weight) - 1e-12))
        if max_single_weight is not None and max_single_weight > 0
        else 1
    )
    start_count = max(int(top_n), int(min_holdings), cap_count)
    holding_cap = int(max_holdings) if max_holdings is not None else None
    if holding_cap is not None and holding_cap <= 0:
        raise ValueError("最大持仓数必须为正整数")
    if holding_cap is not None and holding_cap < int(min_holdings):
        raise ValueError("最大持仓数不能小于最小持仓数")
    tier_names = ("primary", "percentile_relaxed", "hard_only")

    for day in factor.index:
        best_sum = -1.0
        best_selected = pd.Series(False, index=factor.columns)
        best_weights = pd.Series(0.0, index=factor.columns)
        best_tier = "unfilled"
        for tier_name, mask in zip(tier_names, masks):
            eligible = mask.loc[day].fillna(False)
            count = int(eligible.sum())
            if count < int(min_holdings) and tier_name != "hard_only":
                continue
            limit = min(count, max(int(fallback_top_n), start_count))
            if holding_cap is not None:
                limit = min(limit, holding_cap)
            if limit <= 0:
                continue
            ranked = factor.loc[day].where(eligible).rank(
                ascending=False, method="first", na_option="bottom"
            )
            first = min(max(start_count, min(int(min_holdings), count)), limit)
            for holding_count in range(first, limit + 1):
                row_selected = eligible & ranked.le(holding_count)
                row_frame = pd.DataFrame([factor.loc[day]], index=[day])
                row_mask = pd.DataFrame([row_selected], index=[day])
                row_mv = total_mv.loc[[day]] if total_mv is not None else None
                allocated = compute_constrained_weight_by_factor(
                    row_frame,
                    selected=row_mask,
                    max_weight=max_single_weight,
                    industries=industries,
                    industry_max=industry_max,
                    total_mv=row_mv,
                    small_cap_weight_max=small_cap_weight_max,
                    target_weight=target,
                ).iloc[0]
                invested = float(allocated.sum())
                if invested > best_sum:
                    best_sum = invested
                    best_selected = row_selected
                    best_weights = allocated
                    best_tier = tier_name
                if invested >= target - 1e-8:
                    break
            if best_sum >= target - 1e-8:
                break
        selected.loc[day] = best_selected
        weights.loc[day] = best_weights
        tiers.loc[day] = best_tier
        actual_holdings = int(best_weights.gt(1e-12).sum())
        if not allow_partial and (
            best_sum < target - 1e-8 or actual_holdings < int(min_holdings)
        ):
            raise ValueError(
                f"{pd.Timestamp(day).date()} 无法在约束下满足满仓/最小持仓："
                f"投资比例={max(best_sum, 0.0):.6f}，持仓={actual_holdings}"
            )
    return selected, weights, tiers


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
