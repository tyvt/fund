# -*- coding: utf-8 -*-
"""桥接层：复用现有回测数据与选股流水线，输出 RQAlpha 目标权重。"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pandas as pd

from dividend_lowvol_rotation.backtest import BacktestContext, prepare_backtest_context
from dividend_lowvol_rotation.config import (
    BACKTEST_PREFETCH_SIZE,
    BACKTEST_REBALANCE_MODE,
    INDEX_DIVIDEND_WEIGHTING,
    MARKET_VALUATION_ENABLED,
    SELL_MODE,
    TOP_N_BUY,
    resolve_sell_rank,
)
from dividend_lowvol_rotation.dynamic_params import resolve_dynamic_params
from dividend_lowvol_rotation.index_portfolio import (
    build_index_target_codes,
    target_weights_for_portfolio,
)
from dividend_lowvol_rotation.index_retention import (
    enrich_panel_with_holdings,
    should_sell_index_rules,
)
from dividend_lowvol_rotation.market_valuation import valuation_regime
from dividend_lowvol_rotation.rebalance_schedule import resolve_rebalance_dates
from dividend_lowvol_rotation.risk_regime import (
    estimate_portfolio_vol_pct,
    resolve_position_scale,
)
from dividend_lowvol_rotation.scoring import run_screening
from dividend_lowvol_rotation.strategy_params import StrategyParams


@dataclass
class RebalancePlan:
    """单日调仓计划。"""

    as_of: pd.Timestamp
    target_codes: list[str] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)
    sell_codes: list[str] = field(default_factory=list)
    effective_top_n: int = TOP_N_BUY
    position_scale: float = 1.0
    filter_stats: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    ranked: pd.DataFrame | None = None
    buy_pool: pd.DataFrame | None = None
    panel: pd.DataFrame | None = None


def prepare_rqalpha_context(
    start: str,
    end: str,
    *,
    prefetch_size: int = BACKTEST_PREFETCH_SIZE,
    rebalance_mode: str | None = None,
    verbose: bool = True,
) -> tuple[BacktestContext, list[pd.Timestamp]]:
    """预加载与原生回测一致的 BacktestContext 与调仓日列表。"""
    mode = rebalance_mode or BACKTEST_REBALANCE_MODE
    ctx = prepare_backtest_context(
        start,
        end,
        prefetch_size=prefetch_size,
        rebalance_mode=mode,
        verbose=verbose,
    )
    entry_anchor = pd.Timestamp(ctx.calendar[0]) if ctx.calendar else pd.Timestamp(start)
    reb_dates = resolve_rebalance_dates(
        ctx.calendar,
        mode=mode,
        entry_anchor=entry_anchor,
    )
    return ctx, reb_dates


def _held_codes_from_weights(weights: dict[str, float]) -> list[str]:
    return [c for c, w in weights.items() if w and w > 1e-9]


def resolve_rebalance_portfolio_metrics(
    ctx: BacktestContext,
    holdings: dict[str, int],
    cash: float,
    panel: pd.DataFrame,
    as_of: pd.Timestamp,
    *,
    rebalance_mode: str | None = None,
    strategy_params: StrategyParams | None = None,
    price_fn=None,
) -> tuple[float, float, list[str]]:
    """与 backtest.py 调仓前一致：port_value（卖前总市值）+ position_scale。"""
    from types import SimpleNamespace

    from dividend_lowvol_rotation.dynamic_params import resolve_dynamic_params
    from dividend_lowvol_rotation.risk_regime import (
        estimate_portfolio_vol_pct,
        resolve_position_scale,
    )

    mode = rebalance_mode or BACKTEST_REBALANCE_MODE
    lots_proxy = {
        str(code): SimpleNamespace(shares=float(int(shares)))
        for code, shares in holdings.items()
        if shares and int(shares) > 0
    }
    equity = 0.0
    for code, lot in lots_proxy.items():
        if price_fn is not None:
            px = price_fn(code)
        else:
            from dividend_lowvol_rotation.rqalpha.native_rebalance import (
                _trade_price as rebalance_trade_price,
            )

            metrics = ctx.store.metrics_at(code, as_of)
            px = rebalance_trade_price(
                code, panel, as_of, "buy", ctx.store, metrics=metrics
            )
            if px is None or px <= 0:
                px = ctx.store.price_at(code, as_of)
        if px and px > 0:
            equity += lot.shares * float(px)
    port_value = float(cash) + equity

    dynamic = resolve_dynamic_params(
        panel, as_of=as_of, strategy_params=strategy_params, rebalance_mode=mode
    )
    portfolio_vol = (
        estimate_portfolio_vol_pct(lots_proxy, ctx.store, as_of, panel)
        if lots_proxy
        else None
    )
    position_scale, notes = resolve_position_scale(
        market_vol_median_pct=dynamic.market_vol_median_pct,
        panel=panel,
        portfolio_vol_pct=portfolio_vol,
    )
    return port_value, position_scale, notes


def compute_rebalance_plan(
    ctx: BacktestContext,
    as_of: pd.Timestamp,
    *,
    current_weights: dict[str, float] | None = None,
    current_shares: dict[str, int] | None = None,
    top_n: int = TOP_N_BUY,
    sell_rank: int | None = None,
    prefetch_size: int = BACKTEST_PREFETCH_SIZE,
    rebalance_mode: str | None = None,
    strategy_params: StrategyParams | None = None,
) -> RebalancePlan:
    """在指定日期计算目标组合与权重。

    与 backtest.py 对齐：
    - run_screening 使用 effective_top_n（缩小买入池）
    - build_index_target_codes 使用完整 top_n（波动率降仓只缩资金，不强制减持股数）
    - 权重 = 股息率加权 × position_scale（等同 target_equity = port_value × scale）
    - index_rules 调出先于目标组合构建
    """
    mode = rebalance_mode or BACKTEST_REBALANCE_MODE
    sell_rank = resolve_sell_rank(top_n, sell_rank)
    held = _held_codes_from_weights(current_weights or {})
    plan = RebalancePlan(as_of=as_of.normalize())

    panel = ctx.panel_at(as_of, prefetch_size)
    if panel.empty:
        plan.notes.append("候选面板为空，跳过调仓")
        return plan

    dynamic = resolve_dynamic_params(
        panel,
        as_of=as_of,
        strategy_params=strategy_params,
        rebalance_mode=mode,
    )
    portfolio_vol = None
    share_map = {
        str(c): int(s)
        for c, s in (current_shares or {}).items()
        if s and int(s) > 0
    }
    if share_map:
        lots_proxy = {
            c: SimpleNamespace(shares=float(s))
            for c, s in share_map.items()
        }
        portfolio_vol = estimate_portfolio_vol_pct(lots_proxy, ctx.store, as_of, panel)
    elif held and current_weights:
        # 无股数时退化为权重代理（仅用于早期/空持仓，精度较差）
        lots_proxy = {
            c: SimpleNamespace(shares=float(w))
            for c, w in current_weights.items()
            if w and w > 1e-9
        }
        portfolio_vol = estimate_portfolio_vol_pct(lots_proxy, ctx.store, as_of, panel)

    position_scale, scale_notes = resolve_position_scale(
        market_vol_median_pct=dynamic.market_vol_median_pct,
        panel=panel,
        portfolio_vol_pct=portfolio_vol,
    )
    effective_top_n = max(3, int(round(top_n * position_scale)))
    plan.position_scale = position_scale
    plan.effective_top_n = effective_top_n
    plan.notes.extend(scale_notes)

    val_regime = {"pause_new_buys": False}
    if MARKET_VALUATION_ENABLED:
        val_regime = valuation_regime(as_of, ctx.market_pe_hist)

    ranked, buy_pool, filter_stats = run_screening(
        panel,
        top_n=effective_top_n,
        sell_rank=sell_rank,
        dynamic=dynamic,
        as_of=as_of,
        strategy_params=strategy_params,
        rebalance_mode=mode,
    )
    plan.filter_stats = filter_stats
    plan.ranked = ranked
    plan.buy_pool = buy_pool
    plan.panel = panel
    if ranked.empty:
        plan.notes.append("筛选后无合格标的")
        return plan

    if val_regime.get("pause_new_buys"):
        buy_pool = buy_pool.iloc[0:0]
        plan.notes.append("全市场高估：暂停新增买入")

    # 1) index_rules 调出（先于目标组合，与 backtest.py 一致）
    sell_codes: list[str] = []
    held_after_rules = list(held)
    if SELL_MODE == "index_rules" and held:
        retention_panel = enrich_panel_with_holdings(
            panel,
            {c: SimpleNamespace() for c in held},
            store=ctx.store,
            records=ctx.records,
            as_of=as_of,
            risk_hist=ctx.risk_hist,
            div_index=ctx.dividend_year_index,
        )
        for code in held:
            do_sell, _reason = should_sell_index_rules(code, retention_panel)
            if do_sell:
                sell_codes.append(code)
        held_after_rules = [c for c in held if c not in sell_codes]

    # 2) 目标组合：完整 top_n，与 _apply_index_dividend_rebalance(top_n=top_n) 一致
    target_codes = build_index_target_codes(
        held_after_rules, buy_pool, top_n, ranked=ranked
    )
    plan.target_codes = target_codes

    # 3) 股息率加权 × 仓位缩放
    if INDEX_DIVIDEND_WEIGHTING and target_codes:
        raw_weights = target_weights_for_portfolio(target_codes, ranked, panel)
        plan.weights = {c: w * position_scale for c, w in raw_weights.items()}
    elif target_codes:
        eq = position_scale / len(target_codes)
        plan.weights = {c: eq for c in target_codes}

    # 4) 调出不在目标组合内的老持仓
    for code in held:
        if code not in target_codes and code not in sell_codes:
            sell_codes.append(code)
    plan.sell_codes = sell_codes

    return plan
