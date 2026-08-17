"""策略流水线：分红 → 行情 → 波动率 → 动态参数 → 风控 → 打分。"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from dividend_lowvol_rotation.config import (
    DIVIDEND_YIELD_MODE,
    INDUSTRY_CAP_ENABLED,
    MARKET_VALUATION_ENABLED,
    MAX_INDUSTRY_WEIGHT,
    MIN_DIVIDEND_YIELD_PCT,
    MV_TIER_CAP_ENABLED,
    MV_TIER_LARGE_CNY,
    MV_TIER_SMALL_MAX_WEIGHT,
    RISK_FILTER_ENABLED,
    TOP_N_BUY,
    resolve_sell_rank,
)
from dividend_lowvol_rotation.dividend import build_dividend_panel, load_fhps_all_records
from dividend_lowvol_rotation.dynamic_params import resolve_dynamic_params
from dividend_lowvol_rotation.enhanced_factors import attach_enhanced_factors
from dividend_lowvol_rotation.fundamental_factors import attach_quality_momentum
from dividend_lowvol_rotation.market_valuation import load_market_pe_history, valuation_regime
from dividend_lowvol_rotation.risk_screening import (
    attach_risk_from_records,
    batch_load_risk_history,
    merge_risk_history,
    risk_pass_rate_by_industry,
)
from dividend_lowvol_rotation.industry import attach_industry, load_industry_table
from dividend_lowvol_rotation.market_cap import attach_market_fields, market_fields_needed
from dividend_lowvol_rotation.prices import batch_load_volatility
from dividend_lowvol_rotation.quotes import fetch_stock_quotes
from dividend_lowvol_rotation.scoring import dynamic_dividend_yield_pct, run_screening
from dividend_lowvol_rotation.symbols import is_excluded_name


def build_candidate_universe(dividends: pd.DataFrame) -> pd.DataFrame:
    df = dividends.copy()
    df = df[~df["name"].map(is_excluded_name)]
    return df.reset_index(drop=True)


def build_market_panel(
    refresh: bool = False,
    *,
    top_n: int | None = None,
    sell_rank: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    top_n = top_n if top_n is not None else TOP_N_BUY
    sell_rank = resolve_sell_rank(top_n, sell_rank)

    meta: dict = {
        "steps": [],
        "warnings": [],
        "filters": {},
        "top_n": top_n,
        "sell_rank": sell_rank,
        "dynamic": {},
    }
    t0 = datetime.now()

    dividends = build_dividend_panel(refresh=refresh)
    mode_note = DIVIDEND_YIELD_MODE
    if "dividend_mode" in dividends.columns:
        modes = dividends["dividend_mode"].value_counts().to_dict()
        mode_note = f"{DIVIDEND_YIELD_MODE} ({modes})"
    meta["steps"].append(f"分红面板：{len(dividends)} 只（模式 {mode_note}）")
    if dividends.empty:
        meta["warnings"].append("未获取到分红数据")
        return pd.DataFrame(), pd.DataFrame(), meta

    universe = build_candidate_universe(dividends)
    codes = universe["code"].tolist()
    meta["steps"].append(f"初筛股票池：{len(codes)} 只")

    quotes = fetch_stock_quotes(codes)
    meta["steps"].append(f"腾讯实时行情：{len(quotes)}/{len(codes)} 只")

    quote_map = {row["code"]: row for _, row in universe.iterrows()}
    quote_rows = []
    for code in codes:
        q = quotes.get(code)
        if q is None:
            continue
        base = quote_map.get(code, {})
        quote_rows.append(
            {
                "code": code,
                "name": q.name or base.get("name", ""),
                "price": q.price,
                "prev_close": q.prev_close,
                "quote_time": q.quote_time,
            }
        )
    quote_df = pd.DataFrame(quote_rows)
    if quote_df.empty:
        meta["warnings"].append("未获取到有效实时行情")
        return pd.DataFrame(), pd.DataFrame(), meta

    panel = universe.merge(quote_df, on="code", how="inner", suffixes=("_div", ""))
    if "name_div" in panel.columns:
        panel["name"] = panel["name"].where(
            panel["name"].astype(str).str.len() > 0, panel["name_div"]
        )
        panel = panel.drop(columns=["name_div"], errors="ignore")

    panel["dividend_yield_pct"] = panel.apply(
        lambda r: dynamic_dividend_yield_pct(r["cash_per_share"], r["price"]),
        axis=1,
    )
    pre_yield = MIN_DIVIDEND_YIELD_PCT * 0.8
    pre_filter = panel[panel["dividend_yield_pct"] >= pre_yield]
    vol_codes = pre_filter["code"].tolist()
    meta["steps"].append(f"股息率预筛（≥{pre_yield:.1f}%）：{len(vol_codes)} 只，拉取 K 线…")

    vol_df = batch_load_volatility(vol_codes, refresh=refresh)
    meta["steps"].append(f"Baostock {len(vol_df)} 只完成波动率")
    panel = panel.merge(vol_df, on="code", how="inner")

    if market_fields_needed():
        panel = attach_market_fields(panel, as_of=pd.Timestamp.now())

    dynamic = resolve_dynamic_params(panel)
    meta["dynamic"] = {
        "min_yield_pct": dynamic.min_dividend_yield_pct,
        "max_vol_pct": dynamic.max_annualized_vol_pct,
        "yield_weight": dynamic.yield_rank_weight,
        "vol_weight": dynamic.vol_rank_weight,
        "bond_yield_pct": dynamic.bond_yield_pct,
        "market_vol_median_pct": dynamic.market_vol_median_pct,
        "notes": dynamic.notes,
    }
    for note in dynamic.notes:
        meta["steps"].append(note)

    panel = attach_industry(panel, refresh=refresh)
    _mapping, ind_src = load_industry_table(refresh=False)
    meta["steps"].append(f"行业分类：{ind_src}")

    fhps_records = load_fhps_all_records(refresh=False)
    panel = attach_risk_from_records(panel, fhps_records)
    risk_hist = batch_load_risk_history(panel["code"].tolist(), refresh=refresh)
    if not risk_hist.empty:
        panel = merge_risk_history(panel, risk_hist)
        meta["steps"].append(f"排雷指标：{len(risk_hist['code'].unique())} 只")

    as_of = pd.Timestamp.now()
    if not risk_hist.empty:
        panel = attach_quality_momentum(panel, risk_hist, as_of)
    panel = attach_enhanced_factors(
        panel,
        records=fhps_records,
        risk_hist=risk_hist if not risk_hist.empty else None,
        as_of=as_of,
    )

    if RISK_FILTER_ENABLED and "industry" in panel.columns:
        risk_ind = risk_pass_rate_by_industry(panel)
        if not risk_ind.empty:
            meta["risk_pass_by_industry"] = risk_ind.to_dict("records")
            low_pass = risk_ind[risk_ind["pass_rate_pct"] < 50].head(5)
            if not low_pass.empty:
                low_text = "；".join(
                    f"{r['industry']} {r['pass_rate_pct']:.0f}%"
                    for _, r in low_pass.iterrows()
                )
                meta["steps"].append(f"排雷通过率偏低行业：{low_text}")

    val_regime: dict = {}
    if MARKET_VALUATION_ENABLED:
        try:
            pe_hist = load_market_pe_history()
            val_regime = valuation_regime(pd.Timestamp.now(), pe_hist)
            meta["market_valuation"] = val_regime
            if val_regime.get("market_pe_percentile") is not None:
                meta["steps"].append(
                    f"全市场估值锚点：中证800 PE {val_regime.get('market_pe', 0):.2f}，"
                    f"历史分位 {val_regime['market_pe_percentile']:.1f}%"
                    + ("（暂停新买）" if val_regime.get("pause_new_buys") else "")
                )
        except Exception as exc:
            meta["warnings"].append(f"全市场 PE 加载失败：{exc}")

    ranked, buy_pool, filter_stats = run_screening(
        panel,
        top_n=top_n,
        sell_rank=sell_rank,
        dynamic=dynamic,
    )
    if val_regime.get("pause_new_buys") and not buy_pool.empty:
        buy_pool = buy_pool.iloc[0:0]
        meta["steps"].append("全市场高估：暂停新增买入")

    meta["filters"] = filter_stats

    if MV_TIER_CAP_ENABLED:
        max_small = int(round(TOP_N_BUY * MV_TIER_SMALL_MAX_WEIGHT))
        meta["steps"].append(
            f"市值分层：大盘 ≥{MV_TIER_LARGE_CNY / 1e8:.0f}亿，"
            f"中小盘持仓 ≤{MV_TIER_SMALL_MAX_WEIGHT:.0%}（约 {max_small} 只）"
        )
    if INDUSTRY_CAP_ENABLED:
        meta["steps"].append(
            f"行业分散（单行业≤{MAX_INDUSTRY_WEIGHT * 100:.0f}%）：买入池 {len(buy_pool)} 只"
        )
    meta["steps"].append(
        f"候选池：通过全部筛选 **{filter_stats.get('pool_count', filter_stats.get('passed_core_filters', 0))}** 只"
    )
    meta["elapsed_sec"] = round((datetime.now() - t0).total_seconds(), 1)
    meta["as_of"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return ranked, buy_pool, meta
