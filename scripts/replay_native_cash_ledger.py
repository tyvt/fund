# -*- coding: utf-8 -*-
"""重放原生现金台账，对比 2020-01-16 调仓前现金。"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dividend_lowvol_rotation.backtest import PositionLot, prepare_backtest_context, run_backtest
from dividend_lowvol_rotation.config import BACKTEST_PREFETCH_SIZE, TOP_N_BUY
from dividend_lowvol_rotation.corporate_actions import (
    adjust_holdings_for_splits,
    build_split_index,
)
from dividend_lowvol_rotation.costs import resolve_execution_raw_price
from dividend_lowvol_rotation.dividend_tax import accrue_dividend_cash_on_date, build_dividend_index
from dividend_lowvol_rotation.rebalance_schedule import resolve_rebalance_dates
from dividend_lowvol_rotation.rqalpha.bridge import compute_rebalance_plan, resolve_rebalance_portfolio_metrics
from dividend_lowvol_rotation.rqalpha.native_rebalance import simulate_native_rebalance

START = "2016-08-19"
END = "2026-08-19"
RB = pd.Timestamp("2020-01-16")


def main() -> None:
    ctx = prepare_backtest_context(START, END, verbose=False)
    nav, trades, _, _, _, _ = run_backtest(
        start=START, end=END, top_n=TOP_N_BUY, initial_capital=100_000, verbose=False
    )
    nav["date"] = pd.to_datetime(nav["date"])
    native_pre = nav[(nav["date"] == RB) & (nav["holdings_count"] == 7)].iloc[0]
    print("native pre-rb cash", float(native_pre["cash"]))

    split_index = build_split_index(ctx.split_records)
    div_index = build_dividend_index(ctx.dividend_cash_records)
    reb_dates = set(
        d.normalize()
        for d in resolve_rebalance_dates(
            ctx.calendar, mode="index_annual", entry_anchor=pd.Timestamp(START)
        )
    )

    cash = 100_000.0
    rq_hold: dict[str, int] = {}
    buy_dates: dict[str, pd.Timestamp] = {}

    for day in pd.to_datetime(ctx.calendar):
        day = day.normalize()
        if day > RB:
            break

        # 派息（除权日当日按除权前股数）
        if rq_hold:
            lots_div = {
                c: type("L", (), {"shares": s, "buy_date": buy_dates[c], "code": c, "name": ""})()
                for c, s in adjust_holdings_for_splits(
                    rq_hold, split_index, day, buy_dates, include_ex_date=False
                ).items()
            }
            tax, gross, rows = accrue_dividend_cash_on_date(
                lots_div, div_index, day, dividend_cash=True, apply_tax=False, use_payable_date=True
            )
            if gross > 0:
                cash += gross
            _, _, tax_rows = accrue_dividend_cash_on_date(
                lots_div, div_index, day, dividend_cash=True, apply_tax=True, use_payable_date=True
            )
            if tax > 0:
                cash -= tax

        if day == RB:
            print("ledger pre-rb cash", cash)

        if day in reb_dates:
            adj = adjust_holdings_for_splits(
                rq_hold, split_index, day, buy_dates, include_ex_date=True
            )
            panel = ctx.panel_at(day, BACKTEST_PREFETCH_SIZE)
            weights = {}
            for c, s in adj.items():
                px = resolve_execution_raw_price(c, day, ctx.store, panel=panel) or 0
                weights[c] = s * px
            ws = sum(weights.values()) or 1.0
            weights = {c: w / ws for c, w in weights.items()}
            port, scale, _ = resolve_rebalance_portfolio_metrics(
                ctx, adj, cash, panel, day
            )
            plan = compute_rebalance_plan(
                ctx, day, current_weights=weights, current_shares=adj, top_n=TOP_N_BUY
            )
            target_shares, cash, _, orders, _ = simulate_native_rebalance(
                ctx,
                plan,
                holdings=dict(rq_hold),
                buy_dates=buy_dates,
                cash=cash,
                top_n=TOP_N_BUY,
                min_hold_days=365,
                price_map=None,
                port_value_override=port,
                position_scale_override=scale,
            )
            rq_hold = {c: int(s) for c, s in target_shares.items() if int(s) > 0}
            for code in rq_hold:
                if code not in buy_dates:
                    buy_dates[code] = day

    print("delta", cash - float(native_pre["cash"]))


if __name__ == "__main__":
    main()
