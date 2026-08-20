# -*- coding: utf-8 -*-
"""完整重放原生 inter-day 现金至调仓日开盘。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dividend_lowvol_rotation.backtest import PositionLot, prepare_backtest_context, run_backtest
from dividend_lowvol_rotation.config import TOP_N_BUY
from dividend_lowvol_rotation.corporate_actions import apply_splits_on_date, build_split_index
from dividend_lowvol_rotation.dividend_tax import accrue_dividend_cash_on_date, build_dividend_index
from dividend_lowvol_rotation.rebalance_schedule import resolve_rebalance_dates

START = "2016-08-19"
END = "2026-08-19"


def replay_to_rb_morning(rb: pd.Timestamp) -> tuple[float, dict]:
    ctx = prepare_backtest_context(START, END, verbose=False)
    nav, trades, *_ = run_backtest(
        start=START, end=END, top_n=TOP_N_BUY, initial_capital=100_000, verbose=False
    )
    nav["date"] = pd.to_datetime(nav["date"])
    trades["date"] = pd.to_datetime(trades["date"])
    reb_dates = [
        d.normalize()
        for d in resolve_rebalance_dates(
            ctx.calendar, mode="index_annual", entry_anchor=pd.Timestamp(START)
        )
    ]

    split_index = build_split_index(ctx.split_records)
    div_index = build_dividend_index(ctx.dividend_cash_records)

    cash = 100_000.0
    lots: dict[str, PositionLot] = {}

    def credit_split_tax(day: pd.Timestamp) -> None:
        nonlocal cash
        if not lots:
            return
        lots_pre = {
            c: SimpleNamespace(shares=l.shares, buy_date=l.buy_date, code=c, name="")
            for c, l in lots.items()
        }
        _, gross, _ = accrue_dividend_cash_on_date(
            lots_pre, div_index, day, dividend_cash=True, apply_tax=False, use_payable_date=True
        )
        cash += gross
        apply_splits_on_date(lots, split_index, day)
        lots_post = {
            c: SimpleNamespace(shares=l.shares, buy_date=l.buy_date, code=c, name="")
            for c, l in lots.items()
        }
        tax, _, _ = accrue_dividend_cash_on_date(
            lots_post, div_index, day, dividend_cash=True, apply_tax=True, use_payable_date=True
        )
        cash -= tax

    # 成交重放（简化：仅按 trades 表，调仓日末用 nav 校正现金）
    for rb_date in reb_dates:
        if rb_date > rb:
            break
        # inter days since prev rb
        if rb_date == rb:
            # morning of rb only
            credit_split_tax(rb_date)
            break

        # process trades on rb_date
        day_trades = trades[trades["date"].dt.normalize() == rb_date]
        credit_split_tax(rb_date)
        for _, row in day_trades.sort_values("date").iterrows():
            code = str(row["code"])
            sh = int(row["shares"])
            if row["side"] == "买入":
                cash -= float(row["amount"]) + float(row["fee"])
                if code in lots:
                    lots[code].shares += sh
                else:
                    lots[code] = PositionLot(
                        code=code, name="", shares=sh, buy_date=rb_date,
                        buy_price=float(row["price"]), cost_basis=0, buy_fee=0,
                        peak_price=0, prev_price=0,
                    )
            else:
                net = float(row["net_amount"]) if pd.notna(row.get("net_amount")) else float(row["amount"]) - float(row["fee"])
                cash += net
                if code in lots:
                    lots[code].shares -= sh
                    if lots[code].shares <= 0:
                        del lots[code]
        sub = nav[nav["date"] == rb_date].sort_values("holdings_count", ascending=False)
        if not sub.empty:
            cash = float(sub.iloc[0]["cash"])

        # inter days until next rb
        idx = reb_dates.index(rb_date)
        next_rb = reb_dates[idx + 1] if idx + 1 < len(reb_dates) else rb
        cal = [d.normalize() for d in pd.to_datetime(ctx.calendar) if rb_date < d < next_rb]
        for day in cal:
            credit_split_tax(day)

    pre_rows = nav[nav["date"] == rb]
    pre = pre_rows.sort_values("holdings_count", ascending=True).iloc[0]
    return cash, {"native_pre_cash": float(pre["cash"]), "lots": {c: l.shares for c, l in lots.items()}}


if __name__ == "__main__":
    rb = pd.Timestamp("2024-01-16")
    replay_cash, info = replay_to_rb_morning(rb)
    print(f"replay morning cash = {replay_cash:,.2f}")
    print(f"native pre-rb cash   = {info['native_pre_cash']:,.2f}")
    print(f"delta                = {replay_cash - info['native_pre_cash']:,.2f}")
