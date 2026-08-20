# -*- coding: utf-8 -*-
"""快速诊断 2024-01-16：原生 vs simulate 输入/输出。"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("DLV_BACKTEST_PRICE_SOURCE", "rqalpha")

from dividend_lowvol_rotation.backtest import PositionLot, prepare_backtest_context, run_backtest
from dividend_lowvol_rotation.config import BACKTEST_PREFETCH_SIZE, TOP_N_BUY
from dividend_lowvol_rotation.corporate_actions import (
    apply_splits_on_date,
    build_split_index,
)
from dividend_lowvol_rotation.dividend_tax import accrue_dividend_cash_on_date, build_dividend_index
from dividend_lowvol_rotation.rebalance_schedule import resolve_rebalance_dates
from dividend_lowvol_rotation.rqalpha.bridge import (
    compute_rebalance_plan,
    resolve_rebalance_portfolio_metrics,
)
from dividend_lowvol_rotation.rqalpha.native_rebalance import simulate_native_rebalance

RB = pd.Timestamp("2024-01-16")
START = "2016-08-19"
END = "2026-08-19"


def replay_native_state(ctx, trades: pd.DataFrame, nav: pd.DataFrame):
    """重放至 RB 调仓前：含 inter-day 派息/送股/扣税。"""
    cash = 100_000.0
    lots: dict[str, PositionLot] = {}
    reb_dates = {
        d.normalize()
        for d in resolve_rebalance_dates(
            ctx.calendar, mode="index_annual", entry_anchor=pd.Timestamp(START)
        )
    }
    div_index = build_dividend_index(ctx.dividend_cash_records)
    split_index = build_split_index(ctx.split_records)
    cal = [pd.Timestamp(d).normalize() for d in ctx.calendar]

    trade_by_date: dict[pd.Timestamp, list] = {}
    for _, row in trades.sort_values("date").iterrows():
        d = pd.Timestamp(row["date"]).normalize()
        trade_by_date.setdefault(d, []).append(row)

    for day in cal:
        if day >= RB:
            break
        if day in reb_dates:
            pre = {c: SimpleNamespace(shares=l.shares, buy_date=l.buy_date, code=c, name="") for c, l in lots.items()}
            _, gross, _ = accrue_dividend_cash_on_date(
                pre, div_index, day, dividend_cash=True, apply_tax=False, use_payable_date=True
            )
            cash += gross
            apply_splits_on_date(lots, split_index, day)
            post = {c: SimpleNamespace(shares=l.shares, buy_date=l.buy_date, code=c, name="") for c, l in lots.items()}
            tax, _, _ = accrue_dividend_cash_on_date(
                post, div_index, day, dividend_cash=True, apply_tax=True, use_payable_date=True
            )
            cash -= tax

        for row in trade_by_date.get(day, []):
            code = str(row["code"])
            sh = int(row["shares"])
            if row["side"] == "买入":
                cash -= float(row["amount"]) + float(row["fee"])
                if code in lots:
                    lots[code].shares += sh
                    lots[code].cost_basis += float(row["amount"]) + float(row["fee"])
                else:
                    lots[code] = PositionLot(
                        code=code,
                        name="",
                        shares=sh,
                        buy_date=day,
                        buy_price=float(row["price"]),
                        cost_basis=float(row["amount"]) + float(row["fee"]),
                        buy_fee=float(row["fee"]),
                        peak_price=float(row["price"]),
                        prev_price=float(row["price"]),
                    )
            else:
                net = (
                    float(row["net_amount"])
                    if pd.notna(row.get("net_amount"))
                    else float(row["amount"]) - float(row["fee"])
                )
                cash += net
                if code in lots:
                    lots[code].shares -= sh
                    if lots[code].shares <= 0:
                        del lots[code]

    return cash, lots


def main() -> None:
    ctx = prepare_backtest_context(START, END, verbose=False)
    nav, trades, holdings_df, _, _, _ = run_backtest(
        start=START, end=END, top_n=TOP_N_BUY, initial_capital=100_000, verbose=False
    )
    nav["date"] = pd.to_datetime(nav["date"])
    trades["date"] = pd.to_datetime(trades["date"])

    pre_nav = nav[nav["date"] == RB].sort_values("holdings_count", ascending=True).iloc[0]
    native_cash_nav = float(pre_nav["cash"])
    print(f"native pre-rb cash (nav, min holdings) = {native_cash_nav:,.2f}")

    cash, lots = replay_native_state(ctx, trades, nav)
    holdings = {c: int(l.shares) for c, l in lots.items()}
    buy_dates = {c: l.buy_date for c, l in lots.items()}
    print(f"replay pre-rb cash = {cash:,.2f}  delta vs nav = {cash - native_cash_nav:,.2f}")
    print(f"holdings count = {len(holdings)}")

    # 2024 调仓日原生成交
    rb_trades = trades[trades["date"] == RB].copy()
    nat_signed = {}
    for _, r in rb_trades.iterrows():
        s = 1 if r["side"] == "买入" else -1
        nat_signed[str(r["code"])] = nat_signed.get(str(r["code"]), 0) + s * int(r["shares"])
    print(f"\n2024-01-16 native trades ({len(rb_trades)} rows):")
    for c in sorted(nat_signed):
        if c == "600188" or abs(nat_signed[c]) >= 100:
            print(f"  {c}: {nat_signed[c]:+d}")

    # simulate
    panel = ctx.panel_at(RB, BACKTEST_PREFETCH_SIZE)
    weights = {}
    for c, s in holdings.items():
        px = ctx.store.price_at(c, RB) or 0
        weights[c] = s * px
    ws = sum(weights.values()) or 1.0
    weights = {c: w / ws for c, w in weights.items()}

    port, scale, _ = resolve_rebalance_portfolio_metrics(ctx, holdings, cash, panel, RB)
    port_nav, scale_nav, _ = resolve_rebalance_portfolio_metrics(
        ctx, holdings, native_cash_nav, panel, RB
    )
    plan = compute_rebalance_plan(
        ctx, RB, current_weights=weights, current_shares=holdings, top_n=TOP_N_BUY
    )
    plan.position_scale = scale

    trade_rows: list[dict] = []
    _, sim_cash, _, orders, _ = simulate_native_rebalance(
        ctx,
        plan,
        holdings=holdings,
        buy_dates=buy_dates,
        cash=cash,
        top_n=TOP_N_BUY,
        min_hold_days=365,
        port_value_override=port,
        position_scale_override=scale,
        trade_rows=trade_rows,
    )
    sim_signed = {}
    for o in orders:
        sim_signed[o.code] = sim_signed.get(o.code, 0) + o.delta_shares

    print(f"\nsimulate @ replay cash: port={port:,.2f} scale={scale:.6f} sim_cash_after={sim_cash:,.2f}")
    print(f"600188 simulate = {sim_signed.get('600188', 0):+d}  native = {nat_signed.get('600188', 0):+d}")

    # 用 nav 现金再跑
    trade_rows2: list[dict] = []
    _, sim_cash2, _, orders2, _ = simulate_native_rebalance(
        ctx,
        plan,
        holdings=holdings,
        buy_dates=buy_dates,
        cash=native_cash_nav,
        top_n=TOP_N_BUY,
        min_hold_days=365,
        port_value_override=port_nav,
        position_scale_override=scale_nav,
        trade_rows=trade_rows2,
    )
    sim2 = {o.code: o.delta_shares for o in orders2}
    print(f"\nsimulate @ nav cash: port={port_nav:,.2f} 600188 = {sim2.get('600188', 0):+d}")

    diffs = [
        c for c in sorted(set(nat_signed) | set(sim_signed))
        if nat_signed.get(c, 0) != sim_signed.get(c, 0)
    ]
    print(f"\nreplay simulate vs native signed diffs: {len(diffs)}")
    for c in diffs[:15]:
        print(f"  {c}: native {nat_signed.get(c,0):+d} / sim {sim_signed.get(c,0):+d}")


if __name__ == "__main__":
    main()
