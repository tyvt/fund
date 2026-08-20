# -*- coding: utf-8 -*-
"""精查 2020-01-16 000036 原生 vs RQ 买入 100 股差异。"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dividend_lowvol_rotation.backtest import PositionLot, prepare_backtest_context, run_backtest
from dividend_lowvol_rotation.config import (
    BACKTEST_PREFETCH_SIZE,
    BACKTEST_REBALANCE_MODE,
    TOP_N_BUY,
)
from dividend_lowvol_rotation.costs import (
    buy_order_cost,
    max_buy_shares,
    resolve_execution_raw_price,
    settle_sell,
    trade_execution_price,
)
from dividend_lowvol_rotation.dividend_tax import accrue_dividend_cash_on_date, build_dividend_index
from dividend_lowvol_rotation.dynamic_params import resolve_dynamic_params
from dividend_lowvol_rotation.index_portfolio import build_index_target_codes, target_weights_for_portfolio
from dividend_lowvol_rotation.index_retention import enrich_panel_with_holdings, should_sell_index_rules
from dividend_lowvol_rotation.risk_regime import estimate_portfolio_vol_pct, resolve_position_scale
from dividend_lowvol_rotation.rqalpha.bridge import compute_rebalance_plan
from dividend_lowvol_rotation.rqalpha.native_rebalance import simulate_native_rebalance
from dividend_lowvol_rotation.scoring import run_screening

RB = pd.Timestamp("2020-01-16")


def rebuild_state(trades: pd.DataFrame) -> tuple[float, dict[str, PositionLot]]:
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
    return cash, lots


def copy_lot(lot: PositionLot) -> PositionLot:
    return PositionLot(
        code=lot.code,
        name=lot.name,
        shares=lot.shares,
        buy_date=lot.buy_date,
        buy_price=lot.buy_price,
        cost_basis=lot.cost_basis,
        buy_fee=lot.buy_fee,
        peak_price=lot.peak_price,
        prev_price=lot.prev_price,
    )


def native_buy_loop(
    *,
    cash: float,
    lots: dict[str, PositionLot],
    port_value: float,
    position_scale: float,
    target_codes: list[str],
    weight_map: dict[str, float],
    store,
    panel: pd.DataFrame,
) -> tuple[float, dict[str, int]]:
    sim_cash = cash
    sim_lots = {c: copy_lot(l) for c, l in lots.items()}
    bought: dict[str, int] = {}
    target_equity = port_value * position_scale
    for code in sorted(target_codes):
        px = resolve_execution_raw_price(code, RB, store, panel=panel)
        price = trade_execution_price(px, "buy")
        target_mv = target_equity * weight_map[code]
        current_mv = sim_lots[code].shares * px if code in sim_lots else 0.0
        need_mv = target_mv - current_mv
        if need_mv < px * 100:
            continue
        budget = min(need_mv, sim_cash)
        shares = max_buy_shares(budget, price)
        if shares <= 0:
            continue
        _, _, total = buy_order_cost(shares, price)
        if total > sim_cash:
            continue
        bought[code] = shares
        sim_cash -= total
        if code in sim_lots:
            sim_lots[code].shares += shares
    return sim_cash, bought


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

    cash, lots = rebuild_state(trades)
    store = ctx.store
    panel = ctx.panel_at(RB, BACKTEST_PREFETCH_SIZE)
    dynamic = resolve_dynamic_params(panel, as_of=RB, rebalance_mode=BACKTEST_REBALANCE_MODE)
    portfolio_vol = estimate_portfolio_vol_pct(lots, store, RB, panel) if lots else None
    position_scale, scale_notes = resolve_position_scale(
        market_vol_median_pct=dynamic.market_vol_median_pct,
        panel=panel,
        portfolio_vol_pct=portfolio_vol,
    )
    eff_top = max(3, int(round(TOP_N_BUY * position_scale)))
    ranked, buy_pool, _ = run_screening(
        panel,
        top_n=eff_top,
        sell_rank=15,
        dynamic=dynamic,
        as_of=RB,
        rebalance_mode=BACKTEST_REBALANCE_MODE,
    )
    equity = sum(
        lots[c].shares * resolve_execution_raw_price(c, RB, store, panel=panel) for c in lots
    )
    port_value = cash + equity

    div_idx = build_dividend_index(ctx.records)
    lots_ns = {
        c: SimpleNamespace(code=c, shares=l.shares, buy_date=l.buy_date, name="")
        for c, l in lots.items()
    }
    tax, gross, div_rows = accrue_dividend_cash_on_date(
        lots_ns, div_idx, RB, dividend_cash=True, apply_tax=True, use_payable_date=True
    )

    print("=== 2020-01-16 调仓前 ===")
    print(f"现金={cash:,.2f}  port_value={port_value:,.2f}  scale={position_scale:.6f}")
    print(f"派息 gross={gross:.2f}  tax={tax:.2f}  ({len(div_rows)} 笔)")
    if scale_notes:
        print("scale_notes:", scale_notes[:2])

    retention_panel = enrich_panel_with_holdings(
        panel,
        lots,
        store=store,
        records=ctx.records,
        as_of=RB,
        risk_hist=ctx.risk_hist,
        div_index=ctx.dividend_year_index,
    )

    # native: 派息入账 → 扣税 → 卖出 → 买入
    native_cash = cash + gross - tax
    native_lots = {c: copy_lot(l) for c, l in lots.items()}
    sell_proceeds = 0.0
    for code, lot in list(native_lots.items()):
        do_sell, _ = should_sell_index_rules(code, retention_panel)
        if not do_sell:
            continue
        px = resolve_execution_raw_price(code, RB, store, panel=panel)
        price = trade_execution_price(px, "sell")
        fee, stamp, net = settle_sell(lot.shares * price, RB)
        sell_proceeds += net
        native_cash += net
        del native_lots[code]

    target_codes = build_index_target_codes(list(native_lots.keys()), buy_pool, TOP_N_BUY, ranked=ranked)
    weight_map = target_weights_for_portfolio(target_codes, ranked, panel)
    print(f"\n卖出后现金(native路径)={native_cash:,.2f}  (卖出回款 {sell_proceeds:,.2f})")

    _, native_bought = native_buy_loop(
        cash=native_cash,
        lots=native_lots,
        port_value=port_value,
        position_scale=position_scale,
        target_codes=target_codes,
        weight_map=weight_map,
        store=store,
        panel=panel,
    )
    print(f"native 模拟买入 000036={native_bought.get('000036')}")

    # RQ: 测试不同起始现金
    holdings = {c: int(l.shares) for c, l in lots.items()}
    buy_dates = {c: l.buy_date for c, l in lots.items()}
    weights = {
        c: lots[c].shares * resolve_execution_raw_price(c, RB, store, panel=panel) / port_value
        for c in lots
    }
    plan = compute_rebalance_plan(
        ctx, RB, current_weights=weights, current_shares=holdings, top_n=TOP_N_BUY
    )
    print(f"\nplan.position_scale={plan.position_scale:.6f}  native scale={position_scale:.6f}")

    for label, start_cash in [
        ("cash(无派息)", cash),
        ("cash+gross-tax", cash + gross - tax),
        ("cash+gross", cash + gross),
    ]:
        _, _, _, orders, _ = simulate_native_rebalance(
            ctx,
            plan,
            holdings=holdings,
            buy_dates=buy_dates,
            cash=start_cash,
            top_n=TOP_N_BUY,
            min_hold_days=365,
            port_value_override=port_value,
        )
        o36 = next((o for o in orders if o.code == "000036"), None)
        print(f"RQ simulate [{label}] start={start_cash:,.2f} -> 000036 {o36.delta_shares if o36 else None}")

    # 000036 边际分析
    code = "000036"
    px = resolve_execution_raw_price(code, RB, store, panel=panel)
    price = trade_execution_price(px, "buy")
    w = weight_map[code]
    need_mv = port_value * position_scale * w
    print(f"\n000036: price={price} weight={w:.4f} target_mv={need_mv:,.2f}")
    for sh in (5700, 5800, 5900):
        _, fee, total = buy_order_cost(sh, price)
        print(f"  {sh}股 cost={total:,.2f} fee={fee:.2f}")

    # 实际成交
    pkl = pickle.load(open(_ROOT / "output/dividend_lowvol/rqalpha_result.pkl", "rb"))
    rq_tr = pkl["trades"].copy()
    rq_tr["date"] = pd.to_datetime(rq_tr["datetime"]).dt.strftime("%Y-%m-%d")
    nat = trades[(trades["date"] == RB) & (trades["code"] == "000036")]
    rq = rq_tr[
        (rq_tr["date"] == "2020-01-16")
        & (rq_tr["order_book_id"].str.startswith("000036"))
    ]
    print("\n实际 native:", nat[["side", "shares", "amount", "fee"]].to_dict("records"))
    print("实际 RQ:", rq[["side", "last_quantity", "last_price", "commission"]].to_dict("records"))


if __name__ == "__main__":
    main()
