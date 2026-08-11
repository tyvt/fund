"""筛选、排名打分、行业分散与买入价区间。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from dividend_lowvol_rotation.config import (
    BUY_RANGE_ABOVE_LOW_PCT,
    BUY_RANGE_BELOW_CURRENT_PCT,
    EX_DATE_COOLDOWN_DAYS,
    EX_DATE_COOLDOWN_ENABLED,
    INDUSTRY_CAP_ENABLED,
    MAX_ANNUALIZED_VOL_PCT,
    MAX_INDUSTRY_WEIGHT,
    MIN_DIVIDEND_YIELD_PCT,
    SELL_RANK_BUFFER,
    TOP_N_BUY,
    VOL_RANK_WEIGHT,
    YIELD_RANK_WEIGHT,
)
from dividend_lowvol_rotation.dynamic_params import DynamicParams
from dividend_lowvol_rotation.fundamentals import (
    attach_fundamentals_from_fhps,
    fundamental_filter_mask,
    ocf_filter_mask,
)
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
) -> pd.DataFrame:
    out = df.copy()
    out["yield_rank"] = out["dividend_yield_pct"].rank(ascending=False, method="min")
    out["vol_rank"] = out["ann_vol_pct"].rank(ascending=True, method="min")
    out["composite_score"] = out["yield_rank"] * yield_weight + out["vol_rank"] * vol_weight
    out = out.sort_values(["composite_score", "yield_rank", "vol_rank", "code"])
    out["rank"] = range(1, len(out) + 1)
    return out.reset_index(drop=True)


def select_with_industry_cap(
    ranked: pd.DataFrame,
    top_n: int,
    *,
    max_industry_weight: float = MAX_INDUSTRY_WEIGHT,
    industry_cap_enabled: bool = INDUSTRY_CAP_ENABLED,
) -> pd.DataFrame:
    if not industry_cap_enabled or ranked.empty or "industry" not in ranked.columns:
        out = ranked.head(top_n).copy()
        out["portfolio_rank"] = range(1, len(out) + 1)
        return out
    selected = []
    industry_counts: dict[str, int] = {}
    for _, row in ranked.iterrows():
        industry = str(row.get("industry") or "未分类")
        trial_count = industry_counts.get(industry, 0) + 1
        if trial_count / top_n > max_industry_weight:
            continue
        selected.append(row)
        industry_counts[industry] = trial_count
        if len(selected) >= top_n:
            break
    if not selected:
        out = ranked.head(top_n).copy()
        out["portfolio_rank"] = range(1, len(out) + 1)
        return out
    out = pd.DataFrame(selected).reset_index(drop=True)
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


def run_screening(
    panel: pd.DataFrame,
    *,
    top_n: int = TOP_N_BUY,
    sell_rank: int = SELL_RANK_BUFFER,
    dynamic: DynamicParams | None = None,
    as_of: pd.Timestamp | None = None,
    strategy_params: StrategyParams | None = None,
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

    work = attach_fundamentals_from_fhps(panel.copy())
    work = work.dropna(subset=["price", "cash_per_share", "dividend_yield_pct", "ann_vol_pct"])

    ex_mask = ex_date_cooldown_mask(work, as_of=as_of, strategy_params=sp)
    stats["ex_date_cooldown_excluded"] = int((~ex_mask).sum())
    work = work[ex_mask]

    fund_mask = fundamental_filter_mask(work, strategy_params=sp)
    stats["fundamental_excluded"] = int((~fund_mask).sum())
    work = work[fund_mask]

    ocf_mask = ocf_filter_mask(work)
    stats["ocf_excluded"] = int((~ocf_mask).sum())
    work = work[ocf_mask]

    work = work[work["dividend_yield_pct"] >= min_yield]
    work = work[work["ann_vol_pct"] <= max_vol]
    stats["passed_core_filters"] = len(work)
    stats["min_yield_pct"] = min_yield
    stats["max_vol_pct"] = max_vol

    if work.empty:
        return work, work, stats

    ranked = rank_score_panel(work, yield_weight=yield_w, vol_weight=vol_w)
    ranked["yield_cap_price"] = ranked["cash_per_share"].map(
        lambda x: yield_threshold_price(float(x), min_yield)
    )
    ranges = ranked.apply(
        lambda r: suggest_buy_range(r["price"], r.get("low_n"), r.get("yield_cap_price")),
        axis=1,
    )
    ranked["buy_low"] = ranges.map(lambda x: x[0])
    ranked["buy_high"] = ranges.map(lambda x: x[1])
    ranked["in_buy_pool"] = ranked["rank"] <= sell_rank
    ranked["in_top_n"] = ranked["rank"] <= top_n

    max_ind = sp.max_industry_weight if sp.max_industry_weight is not None else MAX_INDUSTRY_WEIGHT
    cap_on = INDUSTRY_CAP_ENABLED if sp.industry_cap_enabled is None else sp.industry_cap_enabled
    buy_pool = select_with_industry_cap(
        ranked, top_n, max_industry_weight=max_ind, industry_cap_enabled=cap_on
    )
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
