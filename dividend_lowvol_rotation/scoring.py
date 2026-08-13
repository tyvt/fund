"""筛选、排名打分、行业分散与买入价区间。"""

from __future__ import annotations

from datetime import date

import math

import numpy as np
import pandas as pd

from dividend_lowvol_rotation.config import (
    ABS_MAX_DEBT_RATIO_PCT,
    ABS_MIN_ROE_PCT,
    BEAR_MAX_VOL_CEILING_PCT,
    BEAR_VOL_THRESHOLD_PCT,
    BUY_RANGE_ABOVE_LOW_PCT,
    BUY_RANGE_BELOW_CURRENT_PCT,
    CANDIDATE_POOL_MIN_RATIO,
    CANDIDATE_POOL_TARGET_RATIO,
    EX_DATE_COOLDOWN_DAYS,
    EX_DATE_COOLDOWN_ENABLED,
    EXPECTED_DIVIDEND_YIELD_ENABLED,
    FILTER_RELAXATION_ENABLED,
    INDEX_STYLE_RANKING,
    INDEX_ANNUAL_REBALANCE_TIMING,
    INDUSTRY_CAP_ENABLED,
    MAX_ANNUALIZED_VOL_PCT,
    MAX_VOL_CEILING_PCT,
    MAX_DEFENSIVE_INDUSTRY_WEIGHT,
    MAX_INDUSTRY_WEIGHT,
    MAX_TOP3_INDUSTRY_WEIGHT,
    MIN_DIVIDEND_YIELD_FLOOR_PCT,
    MIN_DIVIDEND_YIELD_PCT,
    MOMENTUM_HARD_FILTER_ENABLED,
    MOMENTUM_MA_DAYS,
    MOMENTUM_RETURN_DAYS,
    MOMENTUM_SCORE_WEIGHT,
    QUALITY_MOMENTUM_WEIGHT,
    SELL_RANK_BUFFER,
    SOFT_ENHANCED_SCORING_ENABLED,
    SOFT_RISK_SCORING_ENABLED,
    TOP_N_BUY,
    TOP_N_MIN_BUY,
    VALUATION_BUY_ENABLED,
    VALUATION_PB_QUANTILE_NORMAL_PCT,
    VALUATION_PB_QUANTILE_TIGHT_PCT,
    VOL_RANK_WEIGHT,
    YIELD_RANK_WEIGHT,
)
from dividend_lowvol_rotation.industry_caps import (
    beta_balance_ok,
    defensive_weight_ok,
    industry_weight_ok,
    is_defensive_industry,
    is_low_beta,
    top3_weight_ok,
)
from dividend_lowvol_rotation.dynamic_params import DynamicParams
from dividend_lowvol_rotation.fundamentals import (
    fundamental_filter_mask,
)
from dividend_lowvol_rotation.risk_screening import risk_filter_mask, risk_score_penalties
from dividend_lowvol_rotation.enhanced_factors import enhanced_filter_mask, enhanced_score_penalties
from dividend_lowvol_rotation.index_portfolio import index_rank_panel
from dividend_lowvol_rotation.strategy_params import StrategyParams


def dynamic_dividend_yield_pct(cash_per_share: float, price: float) -> float | None:
    if price is None or price <= 0 or cash_per_share is None or cash_per_share <= 0:
        return None
    return cash_per_share / price * 100.0


def yield_threshold_price(cash_per_share: float, min_yield_pct: float = MIN_DIVIDEND_YIELD_PCT) -> float | None:
    if cash_per_share is None or cash_per_share <= 0 or min_yield_pct <= 0:
        return None
    return cash_per_share / (min_yield_pct / 100.0)


def ex_date_cooldown_mask(
    df: pd.DataFrame,
    as_of: pd.Timestamp | None = None,
    *,
    strategy_params: StrategyParams | None = None,
) -> pd.Series:
    sp = strategy_params or StrategyParams()
    cooldown_days = sp.ex_date_cooldown_days if sp.ex_date_cooldown_days is not None else EX_DATE_COOLDOWN_DAYS
    if not EX_DATE_COOLDOWN_ENABLED or "ex_date" not in df.columns:
        return pd.Series(True, index=df.index)
    today = as_of or pd.Timestamp(date.today())
    days = (today - pd.to_datetime(df["ex_date"])).dt.days
    return days.isna() | (days > cooldown_days)


def rank_score_panel(
    df: pd.DataFrame,
    *,
    yield_weight: float = YIELD_RANK_WEIGHT,
    vol_weight: float = VOL_RANK_WEIGHT,
    momentum_weight: float = MOMENTUM_SCORE_WEIGHT,
    quality_weight: float = QUALITY_MOMENTUM_WEIGHT,
    yield_col: str = "dividend_yield_pct",
) -> pd.DataFrame:
    out = df.copy()
    ycol = yield_col if yield_col in out.columns else "dividend_yield_pct"
    out["yield_rank"] = out[ycol].rank(ascending=False, method="min")
    out["vol_rank"] = out["ann_vol_pct"].rank(ascending=True, method="min")
    mom_penalty = pd.Series(0.0, index=out.index)
    price = pd.to_numeric(out.get("price"), errors="coerce")
    if "ma_250" in out.columns:
        ma = pd.to_numeric(out["ma_250"], errors="coerce")
        mom_penalty = mom_penalty + (ma.notna() & price.notna() & (price < ma)).astype(float)
    if "ret_12m" in out.columns:
        ret = pd.to_numeric(out["ret_12m"], errors="coerce")
        mom_penalty = mom_penalty + (ret.notna() & (ret < 0)).astype(float) * 0.5
    out["momentum_penalty"] = mom_penalty
    out["momentum_rank"] = out["momentum_penalty"].rank(ascending=True, method="min")
    composite = (
        out["yield_rank"] * yield_weight
        + out["vol_rank"] * vol_weight
        + out["momentum_rank"] * momentum_weight
    )
    if quality_weight > 0 and "quality_mom_roe_pct" in out.columns:
        q = pd.to_numeric(out["quality_mom_roe_pct"], errors="coerce")
        out["quality_rank"] = q.rank(ascending=False, method="min")
        composite = composite + out["quality_rank"] * quality_weight
    if "quality_penalty" in out.columns:
        qp = pd.to_numeric(out["quality_penalty"], errors="coerce").fillna(0)
        out["penalty_rank"] = qp.rank(ascending=True, method="min")
        composite = composite + out["penalty_rank"] * max(momentum_weight, 0.5)
    out["composite_score"] = composite
    out = out.sort_values(["composite_score", "yield_rank", "vol_rank", "code"])
    out["rank"] = range(1, len(out) + 1)
    return out.reset_index(drop=True)


def _selection_sort_columns() -> tuple[list[str], list[bool]]:
    if INDEX_STYLE_RANKING:
        return ["yield_rank", "spread_pct_rank", "val_pref", "vol_rank", "code"], [
            True,
            True,
            False,
            True,
            True,
        ]
    return (
        ["composite_score", "val_pref", "yield_rank", "vol_rank", "code"],
        [True, False, True, True, True],
    )


def select_with_industry_cap(
    ranked: pd.DataFrame,
    top_n: int,
    *,
    max_industry_weight: float = MAX_INDUSTRY_WEIGHT,
    industry_cap_enabled: bool = INDUSTRY_CAP_ENABLED,
    valuation_pb_quantile_pct: float = VALUATION_PB_QUANTILE_NORMAL_PCT,
) -> pd.DataFrame:
    if ranked.empty:
        return ranked
    candidates = ranked.head(min(top_n * 3, len(ranked))).copy()
    if VALUATION_BUY_ENABLED and "bps" in candidates.columns:
        price = pd.to_numeric(candidates["price"], errors="coerce")
        bps = pd.to_numeric(candidates["bps"], errors="coerce")
        candidates["pb"] = np.where(bps > 0, price / bps, np.nan)
        if "industry" in candidates.columns:
            ind = candidates["industry"].fillna("未分类")
            q = valuation_pb_quantile_pct / 100.0

            def _pb_cap(s: pd.Series) -> float:
                valid = s.dropna()
                if valid.empty:
                    return np.nan
                return float(valid.quantile(q))

            pb_cap = candidates.groupby(ind, group_keys=False)["pb"].transform(_pb_cap)
            candidates["val_pref"] = (
                candidates["pb"].notna() & pb_cap.notna() & (candidates["pb"] <= pb_cap)
            ).astype(int)
        else:
            candidates["val_pref"] = 0
        sort_cols, sort_asc = _selection_sort_columns()
        candidates = candidates.sort_values(sort_cols, ascending=sort_asc)
    if not industry_cap_enabled or "industry" not in candidates.columns:
        slot_target = min(top_n, len(candidates))
        out = candidates.head(slot_target).copy()
        out["portfolio_rank"] = range(1, len(out) + 1)
        return out
    slot_target = min(top_n, len(candidates))
    industries = candidates["industry"].fillna("未分类").astype(str).values
    betas = (
        pd.to_numeric(candidates.get("beta_252"), errors="coerce").values
        if "beta_252" in candidates.columns
        else [None] * len(candidates)
    )
    selected_idx = []
    industry_counts: dict[str, int] = {}
    defensive_count = 0
    low_beta_count = 0
    high_beta_count = 0
    max_def = MAX_DEFENSIVE_INDUSTRY_WEIGHT
    max_top3 = MAX_TOP3_INDUSTRY_WEIGHT
    for i, ind in enumerate(industries):
        beta = float(betas[i]) if betas[i] is not None and pd.notna(betas[i]) else None
        if not industry_weight_ok(industry_counts, ind, slot_target, max_industry_weight):
            continue
        if not defensive_weight_ok(industry_counts, defensive_count, ind, slot_target, max_def):
            continue
        trial_counts = dict(industry_counts)
        trial_counts[ind] = trial_counts.get(ind, 0) + 1
        if not top3_weight_ok(trial_counts, slot_target, max_top3):
            continue
        if not beta_balance_ok(low_beta_count, high_beta_count, beta, slot_target):
            continue
        selected_idx.append(i)
        industry_counts = trial_counts
        if is_defensive_industry(ind):
            defensive_count += 1
        if is_low_beta(beta):
            low_beta_count += 1
        elif beta is not None:
            high_beta_count += 1
        if len(selected_idx) >= slot_target:
            break
    if not selected_idx:
        out = ranked.head(slot_target).copy()
        out["portfolio_rank"] = range(1, len(out) + 1)
        return out
    out = candidates.iloc[selected_idx].copy().reset_index(drop=True)
    out["portfolio_rank"] = range(1, len(out) + 1)
    return out


def suggest_buy_range(
    price: float,
    low_n: float | None,
    yield_cap_price: float | None,
) -> tuple[float | None, float | None]:
    if price is None or price <= 0:
        return None, None
    low = price * (1 - BUY_RANGE_BELOW_CURRENT_PCT) if low_n is None or low_n <= 0 else low_n
    high_candidates = [price * (1 - BUY_RANGE_BELOW_CURRENT_PCT)]
    if yield_cap_price is not None and yield_cap_price > 0:
        high_candidates.append(yield_cap_price)
    if low_n is not None and low_n > 0:
        high_candidates.append(low_n * (1 + BUY_RANGE_ABOVE_LOW_PCT))
    high = min(high_candidates)
    if high < low:
        low, high = high, low
    return round(low, 3), round(high, 3)


def momentum_filter_mask(df: pd.DataFrame) -> pd.Series:
    """硬性动量过滤（默认关闭，改用 composite 软性加分）。"""
    if not MOMENTUM_HARD_FILTER_ENABLED or df.empty:
        return pd.Series(True, index=df.index)
    ok = pd.Series(True, index=df.index)
    price = pd.to_numeric(df.get("price"), errors="coerce")
    if "ma_250" in df.columns:
        ma = pd.to_numeric(df["ma_250"], errors="coerce")
        ok &= ma.isna() | (price > ma)
    if "ret_12m" in df.columns:
        ret = pd.to_numeric(df["ret_12m"], errors="coerce")
        ok &= ret.isna() | (ret > 0)
    return ok


def absolute_quality_floor_mask(df: pd.DataFrame) -> pd.Series:
    """放宽机制不可突破的绝对质量底线。"""
    if df.empty:
        return pd.Series(True, index=df.index)
    ok = pd.Series(True, index=df.index)
    if "roe_pct" in df.columns:
        ok &= df["roe_pct"].isna() | (df["roe_pct"] >= ABS_MIN_ROE_PCT)
    if "debt_ratio_pct" in df.columns:
        ok &= df["debt_ratio_pct"].isna() | (df["debt_ratio_pct"] <= ABS_MAX_DEBT_RATIO_PCT)
    return ok


def _apply_core_filters(
    work: pd.DataFrame,
    *,
    min_yield: float,
    max_vol: float,
    as_of: pd.Timestamp | None,
    strategy_params: StrategyParams | None,
    risk_skip: dict[str, bool],
    fund_skip_profit_yoy: bool,
    enhanced_skip: dict[str, bool] | None = None,
) -> tuple[pd.DataFrame, dict]:
    sp = strategy_params or StrategyParams()
    stats: dict = {"panel_input_count": len(work)}

    ex_mask = ex_date_cooldown_mask(work, as_of=as_of, strategy_params=sp)
    stats["ex_date_cooldown_excluded"] = int((~ex_mask).sum())
    work = work[ex_mask]

    fund_mask = fundamental_filter_mask(work, strategy_params=sp)
    if fund_skip_profit_yoy:
        min_roe = sp.min_roe_pct if sp.min_roe_pct is not None else 11.0
        fund_mask = pd.Series(True, index=work.index)
        if "roe_pct" in work.columns:
            fund_mask &= work["roe_pct"].notna() & (work["roe_pct"] >= min_roe)
    stats["fundamental_excluded"] = int((~fund_mask).sum())
    work = work[fund_mask]

    abs_mask = absolute_quality_floor_mask(work)
    stats["abs_quality_excluded"] = int((~abs_mask).sum())
    work = work[abs_mask]

    mom_mask = momentum_filter_mask(work)
    stats["momentum_excluded"] = int((~mom_mask).sum())
    work = work[mom_mask]

    risk_mask, risk_stats = risk_filter_mask(work, strategy_params=sp, skip=risk_skip)
    for k, v in risk_stats.items():
        stats[k] = v
    if SOFT_RISK_SCORING_ENABLED:
        risk_pen, _ = risk_score_penalties(work, skip=risk_skip)
        work = work.copy()
        work["risk_penalty"] = risk_pen
        stats["risk_excluded"] = 0
    else:
        stats["risk_excluded"] = int((~risk_mask).sum())
        work = work[risk_mask]

    enh_mask = enhanced_filter_mask(work, skip=enhanced_skip)
    if SOFT_ENHANCED_SCORING_ENABLED:
        enh_pen = enhanced_score_penalties(work, skip=enhanced_skip)
        work = work.copy()
        work["enhanced_penalty"] = enh_pen
        risk_pen = work["risk_penalty"] if "risk_penalty" in work.columns else 0.0
        work["quality_penalty"] = risk_pen + enh_pen
        stats["enhanced_excluded"] = 0
        stats["enhanced_penalized"] = int((enh_pen > 0).sum())
    else:
        stats["enhanced_excluded"] = int((~enh_mask).sum())
        work = work[enh_mask]

    work = work[work["dividend_yield_pct"] >= min_yield]
    work = work[work["ann_vol_pct"] <= max_vol]
    stats["passed_core_filters"] = len(work)
    stats["min_yield_pct"] = min_yield
    stats["max_vol_pct"] = max_vol
    return work, stats


def _effective_dividend_yield_series(df: pd.DataFrame) -> pd.Series:
    """筛选/门槛用股息率：预期 → 可持续 → 现货。"""
    raw = pd.to_numeric(df.get("dividend_yield_pct"), errors="coerce")
    out = raw.copy()
    if EXPECTED_DIVIDEND_YIELD_ENABLED and "expected_div_yield_pct" in df.columns:
        exp = pd.to_numeric(df["expected_div_yield_pct"], errors="coerce")
        out = exp.fillna(out)
    if "sustainable_div_yield_pct" in df.columns:
        sus = pd.to_numeric(df["sustainable_div_yield_pct"], errors="coerce")
        out = sus.fillna(out)
    return out


def _apply_effective_dividend_yield(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["dividend_yield_pct"] = _effective_dividend_yield_series(out)
    return out


def _enhanced_relaxation_steps() -> list[tuple[str, dict[str, bool]]]:
    return [
        ("full", {}),
        ("no_trap", {"trap": True}),
        ("no_trap_momentum", {"trap": True, "momentum": True}),
        ("no_trap_mom_stab", {"trap": True, "momentum": True, "stability": True}),
        ("enhanced_off", {"trap": True, "momentum": True, "stability": True, "coverage": True}),
    ]


def _relaxation_steps() -> list[tuple[str, dict[str, bool], bool]]:
    return [
        ("full", {}, False),
        ("relax_payout", {"payout": True}, False),
        ("relax_payout_roe_vol", {"payout": True, "roe_vol": True}, False),
        ("relax_payout_roe_vol_divyears", {"payout": True, "roe_vol": True, "dividend_years": True}, False),
        (
            "relax_payout_roe_vol_divyears_profit",
            {"payout": True, "roe_vol": True, "dividend_years": True},
            True,
        ),
    ]


def run_screening(
    panel: pd.DataFrame,
    *,
    top_n: int = TOP_N_BUY,
    sell_rank: int = SELL_RANK_BUFFER,
    dynamic: DynamicParams | None = None,
    as_of: pd.Timestamp | None = None,
    strategy_params: StrategyParams | None = None,
    valuation_tight: bool = False,
    bear_vol_threshold_pct: float | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    sp = strategy_params or StrategyParams()
    stats: dict = {}
    min_yield = dynamic.min_dividend_yield_pct if dynamic else (
        sp.min_dividend_yield_pct if sp.min_dividend_yield_pct is not None else MIN_DIVIDEND_YIELD_PCT
    )
    max_vol = dynamic.max_annualized_vol_pct if dynamic else (
        sp.max_annualized_vol_pct if sp.max_annualized_vol_pct is not None else MAX_ANNUALIZED_VOL_PCT
    )
    yield_w = dynamic.yield_rank_weight if dynamic else (
        sp.yield_rank_weight if sp.yield_rank_weight is not None else YIELD_RANK_WEIGHT
    )
    vol_w = dynamic.vol_rank_weight if dynamic else (
        sp.vol_rank_weight if sp.vol_rank_weight is not None else VOL_RANK_WEIGHT
    )

    work = panel.copy()
    if "eps" in work.columns and "bps" in work.columns:
        eps = pd.to_numeric(work["eps"], errors="coerce")
        bps = pd.to_numeric(work["bps"], errors="coerce")
        work["roe_pct"] = (bps > 0) * (eps / bps * 100.0)
        work.loc[bps <= 0, "roe_pct"] = None
    if "profit_yoy_pct" not in work.columns:
        work["profit_yoy_pct"] = None
    if (
        INDEX_ANNUAL_REBALANCE_TIMING != "january"
        and dynamic
        and dynamic.market_vol_median_pct is not None
    ):
        bear_thresh = (
            bear_vol_threshold_pct if bear_vol_threshold_pct is not None else BEAR_VOL_THRESHOLD_PCT
        )
        if dynamic.market_vol_median_pct >= bear_thresh:
            max_vol = min(max_vol, BEAR_MAX_VOL_CEILING_PCT)
    work = work.dropna(subset=["price", "cash_per_share", "dividend_yield_pct", "ann_vol_pct"])
    work = _apply_effective_dividend_yield(work)
    base = work

    relaxation_level = "full"
    enhanced_relax = "full"
    best_risk_skip: dict[str, bool] = {}
    best_fund_skip = False
    best_enh_skip: dict[str, bool] = {}
    work, stats = _apply_core_filters(
        base,
        min_yield=min_yield,
        max_vol=max_vol,
        as_of=as_of,
        strategy_params=sp,
        risk_skip={},
        fund_skip_profit_yoy=False,
    )
    min_fill = min(TOP_N_MIN_BUY, top_n)
    pool_min = max(min_fill, int(math.ceil(top_n * CANDIDATE_POOL_MIN_RATIO)))
    pool_target = max(pool_min, int(math.ceil(top_n * CANDIDATE_POOL_TARGET_RATIO)))
    stats["pool_min"] = pool_min
    stats["pool_target"] = pool_target

    if FILTER_RELAXATION_ENABLED and len(work) < pool_min:
        for level_name, risk_skip, fund_skip_yoy in _relaxation_steps()[1:]:
            trial, trial_stats = _apply_core_filters(
                base,
                min_yield=min_yield,
                max_vol=max_vol,
                as_of=as_of,
                strategy_params=sp,
                risk_skip=risk_skip,
                fund_skip_profit_yoy=fund_skip_yoy,
            )
            if len(trial) >= pool_min:
                work, stats = trial, trial_stats
                relaxation_level = level_name
                best_risk_skip = risk_skip
                best_fund_skip = fund_skip_yoy
                break
            if len(trial) > len(work):
                work, stats = trial, trial_stats
                relaxation_level = level_name
                best_risk_skip = risk_skip
                best_fund_skip = fund_skip_yoy

    if len(work) < pool_min:
        for enh_name, enh_skip in _enhanced_relaxation_steps()[1:]:
            trial, trial_stats = _apply_core_filters(
                base,
                min_yield=min_yield,
                max_vol=max_vol,
                as_of=as_of,
                strategy_params=sp,
                risk_skip=best_risk_skip,
                fund_skip_profit_yoy=best_fund_skip,
                enhanced_skip=enh_skip,
            )
            if len(trial) >= pool_min:
                work, stats = trial, trial_stats
                enhanced_relax = enh_name
                best_enh_skip = enh_skip
                break
            if len(trial) > len(work):
                work, stats = trial, trial_stats
                enhanced_relax = enh_name
                best_enh_skip = enh_skip

    if len(work) < pool_min and min_yield > MIN_DIVIDEND_YIELD_FLOOR_PCT:
        for lowered in (
            max(min_yield - 0.25, MIN_DIVIDEND_YIELD_FLOOR_PCT),
            MIN_DIVIDEND_YIELD_FLOOR_PCT,
        ):
            if lowered >= min_yield:
                continue
            trial, trial_stats = _apply_core_filters(
                base,
                min_yield=lowered,
                max_vol=max_vol,
                as_of=as_of,
                strategy_params=sp,
                risk_skip=best_risk_skip,
                fund_skip_profit_yoy=best_fund_skip,
                enhanced_skip=best_enh_skip,
            )
            if len(trial) >= pool_min:
                work, stats = trial, trial_stats
                stats["min_yield_pct"] = lowered
                relaxation_level = f"{relaxation_level}+yield_{lowered:.2f}"
                break
            if len(trial) > len(work):
                work, stats = trial, trial_stats
                stats["min_yield_pct"] = lowered
                relaxation_level = f"{relaxation_level}+yield_{lowered:.2f}"

    if len(work) < pool_min and max_vol < MAX_VOL_CEILING_PCT + 15:
        for vol_cap in (max_vol + 5.0, MAX_ANNUALIZED_VOL_PCT, MAX_VOL_CEILING_PCT + 10.0):
            if vol_cap <= max_vol:
                continue
            trial, trial_stats = _apply_core_filters(
                base,
                min_yield=stats.get("min_yield_pct", min_yield),
                max_vol=vol_cap,
                as_of=as_of,
                strategy_params=sp,
                risk_skip=best_risk_skip,
                fund_skip_profit_yoy=best_fund_skip,
                enhanced_skip=best_enh_skip,
            )
            if len(trial) >= pool_min:
                work, stats = trial, trial_stats
                stats["max_vol_pct"] = vol_cap
                relaxation_level = f"{relaxation_level}+vol_{vol_cap:.0f}"
                break
            if len(trial) > len(work):
                work, stats = trial, trial_stats
                stats["max_vol_pct"] = vol_cap
                relaxation_level = f"{relaxation_level}+vol_{vol_cap:.0f}"

    stats["relaxation_level"] = relaxation_level
    stats["enhanced_relaxation"] = enhanced_relax
    stats["min_fill_target"] = min_fill
    stats["pool_min"] = pool_min
    stats["pool_target"] = pool_target
    stats["qualified_count"] = len(work)
    stats["pool_count"] = len(work)
    stats["pool_sufficient"] = len(work) >= pool_min

    if work.empty:
        return work, work, stats

    if EXPECTED_DIVIDEND_YIELD_ENABLED and "expected_div_yield_pct" in work.columns:
        work["dividend_yield_pct"] = work["expected_div_yield_pct"].fillna(work["dividend_yield_pct"])

    if INDEX_STYLE_RANKING:
        ranked = index_rank_panel(work)
    else:
        ranked = rank_score_panel(
            work,
            yield_weight=yield_w,
            vol_weight=vol_w,
            quality_weight=QUALITY_MOMENTUM_WEIGHT,
        )
    ranked["yield_cap_price"] = ranked["cash_per_share"].map(
        lambda x: yield_threshold_price(float(x), min_yield)
    )
    price = ranked["price"].values
    low_n = ranked["low_n"].values if "low_n" in ranked.columns else None
    ycp = ranked["yield_cap_price"].values

    below_pct = BUY_RANGE_BELOW_CURRENT_PCT
    above_pct = BUY_RANGE_ABOVE_LOW_PCT

    low_default = price * (1 - below_pct)
    low_arr = np.where(
        (low_n is None) | pd.isna(low_n) | (low_n <= 0),
        low_default,
        low_n.astype(float),
    )

    high_default = price * (1 - below_pct)
    high_arr = high_default.copy()
    if low_n is not None:
        mask_ln = (~pd.isna(low_n)) & (low_n > 0)
        high_arr[mask_ln] = np.maximum(high_arr[mask_ln], low_n[mask_ln].astype(float) * (1 + above_pct))
    mask_ycp = (~pd.isna(ycp)) & (ycp > 0)
    high_arr[mask_ycp] = np.maximum(high_arr[mask_ycp], ycp[mask_ycp].astype(float))

    swap = high_arr < low_arr
    final_low = np.where(swap, high_arr, low_arr)
    final_high = np.where(swap, low_arr, high_arr)

    ranked["buy_low"] = np.round(final_low, 3)
    ranked["buy_high"] = np.round(final_high, 3)
    ranked["in_buy_pool"] = ranked["rank"] <= sell_rank
    ranked["in_top_n"] = ranked["rank"] <= top_n

    max_ind = sp.max_industry_weight if sp.max_industry_weight is not None else MAX_INDUSTRY_WEIGHT
    cap_on = INDUSTRY_CAP_ENABLED if sp.industry_cap_enabled is None else sp.industry_cap_enabled
    pb_quantile = (
        VALUATION_PB_QUANTILE_TIGHT_PCT if valuation_tight else VALUATION_PB_QUANTILE_NORMAL_PCT
    )
    slot_target = min(top_n, len(ranked))
    buy_pool = select_with_industry_cap(
        ranked,
        slot_target,
        max_industry_weight=max_ind,
        industry_cap_enabled=cap_on,
        valuation_pb_quantile_pct=pb_quantile,
    )
    stats["valuation_tight"] = valuation_tight
    stats["valuation_pb_quantile_pct"] = pb_quantile
    stats["buy_pool_count"] = len(buy_pool)
    return ranked, buy_pool, stats


def classify_holdings(
    ranked: pd.DataFrame,
    holdings: list[str],
    *,
    top_n: int = TOP_N_BUY,
    sell_rank: int = SELL_RANK_BUFFER,
    buy_pool: pd.DataFrame | None = None,
) -> dict[str, list[str]]:
    if not holdings or ranked.empty:
        return {"buy_new": [], "hold_ok": [], "sell_watch": [], "not_in_pool": []}
    hold_set = set(holdings)
    sub = ranked[ranked["code"].isin(hold_set)].copy()
    if buy_pool is not None and not buy_pool.empty:
        pool_codes = set(buy_pool["code"].tolist())
    else:
        pool_codes = set(ranked[ranked["rank"] <= top_n]["code"].tolist())
    buy_new = [c for c in pool_codes if c not in hold_set]
    hold_ok = sub[sub["rank"] <= sell_rank]["code"].tolist()
    sell_watch = sub[sub["rank"] > sell_rank]["code"].tolist()
    not_in_pool = [c for c in holdings if c not in set(ranked["code"])]
    return {
        "buy_new": buy_new,
        "hold_ok": hold_ok,
        "sell_watch": sell_watch,
        "not_in_pool": not_in_pool,
    }
