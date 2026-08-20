# -*- coding: utf-8 -*-
"""对比 2023 调仓后 ~ 2024 调仓前原生现金与台账重放。"""
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

START = "2016-08-19"
END = "2026-08-19"
RB_PREV = pd.Timestamp("2023-01-16")
RB = pd.Timestamp("2024-01-16")


def rebuild_lots_to(trades: pd.DataFrame, nav: pd.DataFrame, until: pd.Timestamp):
    """重放成交至 until（不含当日），调仓日末现金对齐 nav。"""
    cash = 100_000.0
    lots: dict[str, PositionLot] = {}
    reb_dates = set(pd.to_datetime(nav.loc[nav["holdings_count"] >= 3, "date"]).dt.normalize())

    for _, row in trades.sort_values("date").iterrows():
        d = pd.Timestamp(row["date"]).normalize()
        if d >= until:
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
            sub = nav[nav["date"] == d].sort_values("holdings_count", ascending=False)
            if not sub.empty:
                cash = float(sub.iloc[0]["cash"])
    return cash, lots


def native_morning_cash_on(rb: pd.Timestamp, nav: pd.DataFrame, trades: pd.DataFrame, ctx) -> float:
    """调仓日开盘派息后、成交前现金（重放 native 逻辑）。"""
    cash, lots = rebuild_lots_to(trades, nav, rb)
    split_index = build_split_index(ctx.split_records)
    div_index = build_dividend_index(ctx.dividend_cash_records)

    # 当日派息前股数快照
    lots_pre = {c: SimpleNamespace(shares=l.shares, buy_date=l.buy_date, code=c, name="") for c, l in lots.items()}
    _, gross, _ = accrue_dividend_cash_on_date(
        lots_pre, div_index, rb, dividend_cash=True, apply_tax=False, use_payable_date=True
    )
    cash += gross
    apply_splits_on_date(lots, split_index, rb)
    lots_post = {c: SimpleNamespace(shares=l.shares, buy_date=l.buy_date, code=c, name="") for c, l in lots.items()}
    tax, _, _ = accrue_dividend_cash_on_date(
        lots_post, div_index, rb, dividend_cash=True, apply_tax=True, use_payable_date=True
    )
    cash -= tax
    return cash, lots


def ledger_replay(ctx, trades: pd.DataFrame, nav: pd.DataFrame, from_date: pd.Timestamp, to_date: pd.Timestamp) -> float:
    cash, ts = rebuild_lots_to(trades, nav, from_date)
    # convert lots to ts dict at from_date end
    _, lots = rebuild_lots_to(trades, nav, from_date)
    ts = {c: l.shares for c, l in lots.items()}
    bd = {c: l.buy_date for c, l in lots.items()}

    split_index = build_split_index(ctx.split_records)
    div_index = build_dividend_index(ctx.dividend_cash_records)
    cal = [d.normalize() for d in pd.to_datetime(ctx.calendar) if from_date < d.normalize() <= to_date]

    for day in cal:
        if not ts:
            continue
        pre = dict(ts)
        lots_pre = {
            c: SimpleNamespace(shares=s, buy_date=bd[c], code=c, name="") for c, s in pre.items()
        }
        _, gross, _ = accrue_dividend_cash_on_date(
            lots_pre, div_index, day, dividend_cash=True, apply_tax=False, use_payable_date=True
        )
        cash += gross
        # split on lots dict via apply_splits_to_holdings
        from dividend_lowvol_rotation.corporate_actions import apply_splits_to_holdings

        apply_splits_to_holdings(ts, split_index, day)
        for c, s in ts.items():
            if c in lots and lots[c].shares != s:
                lots[c].shares = s
        post = dict(ts)
        lots_post = {
            c: SimpleNamespace(shares=s, buy_date=bd[c], code=c, name="") for c, s in post.items()
        }
        tax, _, _ = accrue_dividend_cash_on_date(
            lots_post, div_index, day, dividend_cash=True, apply_tax=True, use_payable_date=True
        )
        cash -= tax
    return cash


def main() -> None:
    ctx = prepare_backtest_context(START, END, verbose=False)
    nav, trades, _, _, _, div_tax = run_backtest(
        start=START, end=END, top_n=TOP_N_BUY, initial_capital=100_000, verbose=False
    )
    nav["date"] = pd.to_datetime(nav["date"])
    trades["date"] = pd.to_datetime(trades["date"])

    post_2023 = nav[nav["date"] == RB_PREV].sort_values("holdings_count", ascending=False).iloc[0]
    print(f"2023-01-16 post-rb cash (nav) = {post_2023['cash']:,.2f}")

    morning_cash, lots = native_morning_cash_on(RB, nav, trades, ctx)
    print(f"2024-01-16 morning cash (replayed) = {morning_cash:,.2f}")

    # nav on last day before RB
    before = nav[nav["date"] < RB].iloc[-1]
    print(f"last day before RB ({before['date'].date()}) cash = {before['cash']:,.2f}")

    replay = ledger_replay(ctx, trades, nav, RB_PREV, RB - pd.Timedelta(days=1))
    print(f"ledger replay to day before RB = {replay:,.2f}")
    print(f"delta replay vs morning = {replay - morning_cash:,.2f}")

    # dividend events between 2023-01-17 and 2024-01-15
    if not div_tax.empty:
        div_tax = div_tax.copy()
        div_tax["ex_date"] = pd.to_datetime(div_tax["ex_date"])
        sub = div_tax[(div_tax["ex_date"] > RB_PREV) & (div_tax["ex_date"] < RB)]
        print(f"\ndividend tax events between: {len(sub)} rows, tax sum = {sub['tax_amount'].sum():,.2f}")


if __name__ == "__main__":
    main()
