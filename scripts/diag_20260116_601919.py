# -*- coding: utf-8 -*-
"""诊断 2026-01-16 601919 原生 +100 / RQ +0。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("DLV_BACKTEST_PRICE_SOURCE", "rqalpha")

from dividend_lowvol_rotation.backtest import prepare_backtest_context, run_backtest
from dividend_lowvol_rotation.config import BACKTEST_PREFETCH_SIZE, TOP_N_BUY
from dividend_lowvol_rotation.rqalpha.bridge import compute_rebalance_plan, resolve_rebalance_portfolio_metrics
from dividend_lowvol_rotation.rqalpha.native_rebalance import simulate_native_rebalance

RB = pd.Timestamp("2026-01-16")


def main() -> None:
    ctx = prepare_backtest_context("2016-08-19", "2026-08-19", verbose=False)
    nav, trades, holdings_df, *_ = run_backtest(
        start="2016-08-19", end="2026-08-19", top_n=TOP_N_BUY, initial_capital=100_000, verbose=False
    )
    nav["date"] = pd.to_datetime(nav["date"])
    trades["date"] = pd.to_datetime(trades["date"])
    holdings_df["date"] = pd.to_datetime(holdings_df["date"])

    pre = nav[nav["date"] == RB].sort_values("holdings_count", ascending=True).iloc[0]
    native_cash = float(pre["cash"])
    print(f"native pre-rb cash = {native_cash:,.2f}")

    # pre-rb holdings: trades before RB + splits via holdings from min count row isn't in holdings_df
    lots = {}
    buy_dates = {}
    for _, r in trades.sort_values("date").iterrows():
        d = pd.Timestamp(r["date"]).normalize()
        if d >= RB:
            break
        c = str(r["code"])
        sh = int(r["shares"])
        if r["side"] == "买入":
            lots[c] = lots.get(c, 0) + sh
            if c not in buy_dates:
                buy_dates[c] = d
        else:
            lots[c] = lots.get(c, 0) - sh
            if lots.get(c, 0) <= 0:
                lots.pop(c, None)
                buy_dates.pop(c, None)
    print(f"pre-rb holdings (trades only, no splits): {len(lots)}")

    rb_trades = trades[trades["date"] == RB]
    nat_601919 = rb_trades[rb_trades["code"] == "601919"]
    print("native 601919 trades:")
    print(nat_601919[["side", "shares", "price", "amount", "reason"]].to_string(index=False))

    panel = ctx.panel_at(RB, BACKTEST_PREFETCH_SIZE)
    weights = {}
    for c, s in lots.items():
        px = ctx.store.price_at(c, RB) or 0
        weights[c] = s * px
    ws = sum(weights.values()) or 1
    weights = {c: w / ws for c, w in weights.items()}

    port, scale, _ = resolve_rebalance_portfolio_metrics(ctx, lots, native_cash, panel, RB)
    plan = compute_rebalance_plan(ctx, RB, current_weights=weights, current_shares=lots, top_n=TOP_N_BUY)
    plan.position_scale = scale
    trade_rows = []
    _, sim_cash, _, orders, _ = simulate_native_rebalance(
        ctx, plan, holdings=lots, buy_dates=buy_dates, cash=native_cash,
        top_n=TOP_N_BUY, min_hold_days=365, port_value_override=port,
        position_scale_override=scale, trade_rows=trade_rows,
    )
    sim_601919 = next((o for o in orders if o.code == "601919"), None)
    print(f"\nsimulate 601919 = {sim_601919.delta_shares if sim_601919 else 0:+d}")
    print(f"sim_cash_after = {sim_cash:,.2f}")
    for delta in (-500, -200, -100, -50, 0, 50, 100):
        c = native_cash + delta
        p, sc, _ = resolve_rebalance_portfolio_metrics(ctx, lots, c, panel, RB)
        plan.position_scale = sc
        _, _, _, ords, _ = simulate_native_rebalance(
            ctx, plan, holdings=lots, buy_dates=buy_dates, cash=c,
            top_n=TOP_N_BUY, min_hold_days=365, port_value_override=p,
            position_scale_override=sc,
        )
        sh = next((o.delta_shares for o in ords if o.code == "601919"), 0)
        print(f"  cash {c:,.2f} ({delta:+d}) -> 601919 {sh:+d}")


if __name__ == "__main__":
    main()
