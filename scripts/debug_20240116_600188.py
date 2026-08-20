# -*- coding: utf-8 -*-
"""精查 2024-01-16 600188 原生 700 vs RQ 600。"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dividend_lowvol_rotation.backtest import PositionLot, run_backtest
from dividend_lowvol_rotation.config import BACKTEST_PREFETCH_SIZE, TOP_N_BUY
from dividend_lowvol_rotation.corporate_actions import apply_splits_to_holdings, build_split_index
from dividend_lowvol_rotation.dividend_tax import accrue_dividend_cash_on_date, build_dividend_index
from dividend_lowvol_rotation.rebalance_schedule import resolve_rebalance_dates
from dividend_lowvol_rotation.rqalpha.bridge import compute_rebalance_plan, resolve_rebalance_portfolio_metrics
from dividend_lowvol_rotation.rqalpha.native_rebalance import simulate_native_rebalance
from dividend_lowvol_rotation.backtest import prepare_backtest_context

RB = pd.Timestamp("2024-01-16")
START = "2016-08-19"
END = "2026-08-19"


def rebuild_native_state(trades: pd.DataFrame, nav: pd.DataFrame):
    cash = 100_000.0
    lots: dict[str, PositionLot] = {}
    reb_dates = set(pd.to_datetime(nav.loc[nav["holdings_count"] >= 3, "date"]).dt.normalize())

    for _, row in trades.sort_values("date").iterrows():
        d = pd.Timestamp(row["date"]).normalize()
        if d >= RB:
            break
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
                    buy_date=d,
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
        if d in reb_dates:
            row_nav = nav[nav["date"] == d].sort_values("holdings_count", ascending=False)
            if not row_nav.empty:
                cash = float(row_nav.iloc[0]["cash"])

    return cash, lots


def replay_ledger_cash(ctx, trades: pd.DataFrame, nav: pd.DataFrame) -> float:
    """按策略口径重放现金台账至 RB 调仓前。"""
    div_index = build_dividend_index(ctx.dividend_cash_records)
    split_index = build_split_index(ctx.split_records)
    reb_dates = {
        d.normalize()
        for d in resolve_rebalance_dates(
            ctx.calendar, mode="index_annual", entry_anchor=pd.Timestamp(START)
        )
    }

    cash = 100_000.0
    ts: dict[str, int] = {}
    bd: dict[str, pd.Timestamp] = {}

    for _, row in trades.sort_values("date").iterrows():
        d = pd.Timestamp(row["date"]).normalize()
        if d >= RB:
            break
        code = str(row["code"])
        sh = int(row["shares"])
        if row["side"] == "买入":
            ts[code] = ts.get(code, 0) + sh
            if code not in bd:
                bd[code] = d
        else:
            ts[code] = ts.get(code, 0) - sh
            if ts.get(code, 0) <= 0:
                ts.pop(code, None)
                bd.pop(code, None)
        if d in reb_dates:
            row_nav = nav[nav["date"] == d].sort_values("holdings_count", ascending=False)
            if not row_nav.empty:
                cash = float(row_nav.iloc[0]["cash"])

    for day in pd.to_datetime(ctx.calendar):
        day = day.normalize()
        if day >= RB:
            break
        if day in reb_dates:
            continue
        if not ts:
            continue
        pre = dict(ts)
        lots_pre = {
            c: type("L", (), {"shares": s, "buy_date": bd[c], "code": c, "name": ""})()
            for c, s in pre.items()
        }
        _, gross, _ = accrue_dividend_cash_on_date(
            lots_pre, div_index, day, dividend_cash=True, apply_tax=False, use_payable_date=True
        )
        cash += gross
        apply_splits_to_holdings(ts, split_index, day)
        post = dict(ts)
        lots_post = {
            c: type("L", (), {"shares": s, "buy_date": bd[c], "code": c, "name": ""})()
            for c, s in post.items()
        }
        tax, _, _ = accrue_dividend_cash_on_date(
            lots_post, div_index, day, dividend_cash=True, apply_tax=True, use_payable_date=True
        )
        cash -= tax

    return cash


def simulate_buy_600188(ctx, cash: float, lots: dict[str, PositionLot], buy_dates: dict):
    holdings = {c: int(l.shares) for c, l in lots.items()}
    weights = {}
    panel = ctx.panel_at(RB, BACKTEST_PREFETCH_SIZE)
    for c, s in holdings.items():
        px = ctx.store.price_at(c, RB) or 0
        weights[c] = s * px
    ws = sum(weights.values()) or 1.0
    weights = {c: w / ws for c, w in weights.items()}

    port, scale, _ = resolve_rebalance_portfolio_metrics(ctx, holdings, cash, panel, RB)
    plan = compute_rebalance_plan(
        ctx, RB, current_weights=weights, current_shares=holdings, top_n=TOP_N_BUY
    )
    plan.position_scale = scale
    _, _, _, orders, _ = simulate_native_rebalance(
        ctx,
        plan,
        holdings=holdings,
        buy_dates=buy_dates,
        cash=cash,
        top_n=TOP_N_BUY,
        min_hold_days=365,
        price_map=None,
        port_value_override=port,
        position_scale_override=scale,
    )
    o = next((x for x in orders if x.code == "600188"), None)
    return o.delta_shares if o else None, port, scale


def main() -> None:
    ctx = prepare_backtest_context(START, END, verbose=False)
    nav, trades, *_ = run_backtest(
        start=START, end=END, top_n=TOP_N_BUY, initial_capital=100_000, verbose=False
    )
    nav["date"] = pd.to_datetime(nav["date"])
    trades["date"] = pd.to_datetime(trades["date"])

    pre = nav[nav["date"] == RB].sort_values("holdings_count", ascending=False).iloc[0]
    native_cash = float(pre["cash"])
    cash, lots = rebuild_native_state(trades, nav)
    buy_dates = {c: l.buy_date for c, l in lots.items()}
    ledger_cash = replay_ledger_cash(ctx, trades, nav)

    print(f"native pre-rb cash (nav) = {native_cash:,.2f}")
    print(f"rebuild cash (trades only) = {cash:,.2f}")
    print(f"ledger replay cash        = {ledger_cash:,.2f}")
    print(f"ledger - native           = {ledger_cash - native_cash:,.2f}")

    sh, port, scale = simulate_buy_600188(ctx, native_cash, lots, buy_dates)
    print(f"\nsimulate @ native cash -> 600188 = {sh}  port={port:,.2f} scale={scale:.6f}")

    for delta in (-2000, -1500, -1000, -500, 0, 500):
        c = native_cash + delta
        s, _, _ = simulate_buy_600188(ctx, c, lots, buy_dates)
        print(f"  cash {c:,.2f} ({delta:+d}) -> 600188 {s}")


if __name__ == "__main__":
    main()
