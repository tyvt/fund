# -*- coding: utf-8 -*-
"""逐调仓日对比原生 nav 现金与台账重放。"""
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
from dividend_lowvol_rotation.config import TOP_N_BUY
from dividend_lowvol_rotation.corporate_actions import apply_splits_on_date, build_split_index
from dividend_lowvol_rotation.dividend_tax import accrue_dividend_cash_on_date, build_dividend_index
from dividend_lowvol_rotation.rebalance_schedule import resolve_rebalance_dates

START = "2016-08-19"
END = "2026-08-19"


def replay_ledger(ctx, trades: pd.DataFrame, nav: pd.DataFrame):
    cash = 100_000.0
    lots: dict[str, PositionLot] = {}
    div_index = build_dividend_index(ctx.dividend_cash_records)
    split_index = build_split_index(ctx.split_records)
    reb_dates = sorted(
        d.normalize()
        for d in resolve_rebalance_dates(
            ctx.calendar, mode="index_annual", entry_anchor=pd.Timestamp(START)
        )
    )
    trade_by_date: dict[pd.Timestamp, list] = {}
    for _, row in trades.sort_values("date").iterrows():
        d = pd.Timestamp(row["date"]).normalize()
        trade_by_date.setdefault(d, []).append(row)

    nav["date"] = pd.to_datetime(nav["date"])
    nav_by_date = {}
    for d, grp in nav.groupby("date"):
        grp = grp.sort_values("holdings_count")
        nav_by_date[pd.Timestamp(d).normalize()] = {
            "pre_cash": float(grp.iloc[0]["cash"]),
            "post_cash": float(grp.iloc[-1]["cash"]),
            "pre_n": int(grp.iloc[0]["holdings_count"]),
            "post_n": int(grp.iloc[-1]["holdings_count"]),
        }

    rows = []
    cal = [pd.Timestamp(d).normalize() for d in ctx.calendar]
    for day in cal:
        pre_div_cash = cash
        if lots:
            pre = {
                c: SimpleNamespace(shares=l.shares, buy_date=l.buy_date, code=c, name="")
                for c, l in lots.items()
            }
            _, gross, _ = accrue_dividend_cash_on_date(
                pre, div_index, day, dividend_cash=True, apply_tax=False, use_payable_date=True
            )
            cash += gross
            apply_splits_on_date(lots, split_index, day)
            post = {
                c: SimpleNamespace(shares=l.shares, buy_date=l.buy_date, code=c, name="")
                for c, l in lots.items()
            }
            tax, _, _ = accrue_dividend_cash_on_date(
                post, div_index, day, dividend_cash=True, apply_tax=True, use_payable_date=True
            )
            cash -= tax

        if day in reb_dates:
            nat = nav_by_date.get(day, {})
            rows.append(
                {
                    "date": day.date().isoformat(),
                    "replay_pre_rb": round(cash, 2),
                    "native_pre_rb": nat.get("pre_cash"),
                    "pre_delta": round(cash - nat["pre_cash"], 2) if nat.get("pre_cash") else None,
                    "holdings_pre": len(lots),
                    "native_pre_n": nat.get("pre_n"),
                }
            )

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

        if day in reb_dates and day in nav_by_date:
            cash = nav_by_date[day]["post_cash"]
    return pd.DataFrame(rows)


def main() -> None:
    ctx = prepare_backtest_context(START, END, verbose=False)
    nav, trades, _, _, _, _ = run_backtest(
        start=START, end=END, top_n=TOP_N_BUY, initial_capital=100_000, verbose=False
    )
    trades["date"] = pd.to_datetime(trades["date"])
    df = replay_ledger(ctx, trades, nav)
    print(df.to_string(index=False))
    bad = df[df["pre_delta"].abs() > 0.5] if "pre_delta" in df.columns else df
    if not bad.empty:
        print(f"\nfirst pre-rb drift >0.5: {bad.iloc[0]['date']} delta={bad.iloc[0]['pre_delta']}")


if __name__ == "__main__":
    main()
