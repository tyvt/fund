# -*- coding: utf-8 -*-
"""对比 2020-01-16 原生 vs RQ 调仓前 port_value / position_scale / 模拟买入。"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dividend_lowvol_rotation.backtest import PositionLot, prepare_backtest_context, run_backtest
from dividend_lowvol_rotation.config import BACKTEST_PREFETCH_SIZE, BACKTEST_REBALANCE_MODE, TOP_N_BUY
from dividend_lowvol_rotation.corporate_actions import (
    adjust_holdings_for_splits,
    apply_splits_on_date,
    build_split_index,
)
from dividend_lowvol_rotation.costs import max_buy_shares, resolve_execution_raw_price, trade_execution_price
from dividend_lowvol_rotation.dynamic_params import resolve_dynamic_params
from dividend_lowvol_rotation.index_portfolio import build_index_target_codes, target_weights_for_portfolio
from dividend_lowvol_rotation.index_retention import enrich_panel_with_holdings, should_sell_index_rules
from dividend_lowvol_rotation.risk_regime import estimate_portfolio_vol_pct, resolve_position_scale
from dividend_lowvol_rotation.rqalpha.bridge import compute_rebalance_plan, resolve_rebalance_portfolio_metrics
from dividend_lowvol_rotation.rqalpha.native_rebalance import simulate_native_rebalance

RB = pd.Timestamp("2020-01-16")


def rebuild_native_lots(trades: pd.DataFrame, split_index, calendar_days) -> tuple[float, dict[str, PositionLot]]:
    cash = 100_000.0
    lots: dict[str, PositionLot] = {}
    for _, row in trades.sort_values("date").iterrows():
        d = pd.Timestamp(row["date"])
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

    for day in calendar_days:
        if day >= RB:
            break
        apply_splits_on_date(lots, split_index, day)
    return cash, lots


def native_port_scale(cash, lots, ctx, panel) -> tuple[float, float, float]:
    equity = 0.0
    for code, lot in lots.items():
        raw = resolve_execution_raw_price(code, RB, ctx.store, panel=panel)
        px = trade_execution_price(raw, "buy") if raw and raw > 0 else None
        if px:
            equity += lot.shares * px
    port = cash + equity
    dynamic = resolve_dynamic_params(panel, as_of=RB, rebalance_mode=BACKTEST_REBALANCE_MODE)
    vol = estimate_portfolio_vol_pct(lots, ctx.store, RB, panel) if lots else None
    scale, notes = resolve_position_scale(
        market_vol_median_pct=dynamic.market_vol_median_pct,
        panel=panel,
        portfolio_vol_pct=vol,
    )
    return port, scale, vol or 0.0


def main() -> None:
    ctx = prepare_backtest_context("2016-08-19", "2026-08-19", verbose=False)
    _, trades, _, _, _, _ = run_backtest(
        start="2016-08-19",
        end="2026-08-19",
        top_n=10,
        initial_capital=100_000,
        verbose=False,
    )
    trades["date"] = pd.to_datetime(trades["date"])
    split_index = build_split_index(ctx.split_records)
    cal = [pd.Timestamp(d) for d in ctx.calendar if pd.Timestamp(d) <= RB]
    cash, lots = rebuild_native_lots(trades, split_index, cal)
    panel = ctx.panel_at(RB, BACKTEST_PREFETCH_SIZE)

    port, scale, vol = native_port_scale(cash, lots, ctx, panel)
    print("=== 原生等价状态（交易+逐日送股）===")
    print(f"cash={cash:,.2f}  port={port:,.2f}  scale={scale:.6f}  vol={vol:.2f}%")
    print("lots:", {c: l.shares for c, l in lots.items()})

    # RQ 持仓（成交股数）
    pkl = pickle.load(open(_ROOT / "output/dividend_lowvol/rqalpha_result.pkl", "rb"))
    rq_tr = pkl["trades"].sort_index()
    rq_hold: dict[str, int] = {}
    for dt, row in rq_tr.iterrows():
        d = pd.Timestamp(dt).normalize()
        if d >= RB:
            break
        code = str(row["order_book_id"]).split(".")[0]
        q = int(row["last_quantity"])
        if row["side"] == "BUY":
            rq_hold[code] = rq_hold.get(code, 0) + q
        else:
            rq_hold[code] = rq_hold.get(code, 0) - q
            if rq_hold.get(code, 0) <= 0:
                rq_hold.pop(code, None)
    buy_dates = {c: lots[c].buy_date for c in lots if c in rq_hold}
  # 仍持有的用 native buy_date；RQ 新仓用 native lot
    for c in rq_hold:
        if c not in buy_dates and c in lots:
            buy_dates[c] = lots[c].buy_date

    adj = adjust_holdings_for_splits(rq_hold, split_index, RB, buy_dates)
    port2, scale2, notes2 = resolve_rebalance_portfolio_metrics(
        ctx, adj, cash, panel, RB, rebalance_mode=BACKTEST_REBALANCE_MODE
    )
    print("\n=== RQ adjust_holdings + resolve_rebalance_portfolio_metrics ===")
    print(f"port={port2:,.2f}  scale={scale2:.6f}")
    print("rq_hold:", rq_hold)
    print("adj:", adj)
    if notes2:
        print("notes:", notes2[:2])

    weights = _current_weight_map = {
        c: rq_hold[c] * (resolve_execution_raw_price(c, RB, ctx.store, panel=panel) or 0) / port
        for c in rq_hold
    }
    wsum = sum(weights.values())
    weights = {c: w / wsum for c, w in weights.items() if w > 0}

    plan = compute_rebalance_plan(
        ctx, RB, current_weights=weights, current_shares=adj, top_n=TOP_N_BUY
    )
    _, _, _, orders, _ = simulate_native_rebalance(
        ctx,
        plan,
        holdings=rq_hold,
        buy_dates=buy_dates,
        cash=cash,
        top_n=TOP_N_BUY,
        min_hold_days=365,
        port_value_override=port2,
        position_scale_override=scale2,
    )
    buys = {o.code: o.delta_shares for o in orders if o.delta_shares > 0}
    print("\n=== simulate（port2/scale2）买入 ===")
    for c in ("000036", "002110", "600507", "600738"):
        print(f"  {c}: {buys.get(c)}")

    _, _, _, orders_native, _ = simulate_native_rebalance(
        ctx,
        plan,
        holdings={c: int(l.shares) for c, l in lots.items()},
        buy_dates={c: l.buy_date for c, l in lots.items()},
        cash=cash,
        top_n=TOP_N_BUY,
        min_hold_days=365,
        port_value_override=port,
        position_scale_override=scale,
    )
    buys2 = {o.code: o.delta_shares for o in orders_native if o.delta_shares > 0}
    print("\n=== simulate（原生 port/scale + native lots）买入 ===")
    for c in ("000036", "002110", "600507", "600738"):
        print(f"  {c}: {buys2.get(c)}")

    nat = trades[(trades["date"] == RB) & (trades["side"] == "买入")]
  # actual
    print("\n=== 实际成交（native / RQ）===")
    for c in ("000036", "002110", "600507", "600738"):
        n = nat[nat["code"] == c]["shares"].tolist()
        rq = rq_tr[
            (pd.to_datetime(rq_tr.index).normalize() == RB)
            & (rq_tr["order_book_id"].str.startswith(c))
            & (rq_tr["side"] == "BUY")
        ]["last_quantity"].tolist()
        print(f"  {c}: native {n}  RQ {rq}")


if __name__ == "__main__":
    main()
