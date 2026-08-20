# -*- coding: utf-8 -*-
"""原生 backtest 再平衡模拟：整手买卖顺序 + 分红现金计入（与 backtest.py 一致）。"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pandas as pd

from dividend_lowvol_rotation.backtest import BacktestContext, PositionLot, StockStats
from dividend_lowvol_rotation.config import (
    BACKTEST_DIVIDEND_CASH,
    DIVIDEND_TAX_ENABLED,
    TOP_N_BUY,
    uses_rqalpha_price_source,
)
from dividend_lowvol_rotation.costs import (
    max_buy_shares,
    resolve_execution_raw_price,
    settle_sell,
    single_side_commission,
    trade_execution_price,
)
from dividend_lowvol_rotation.dividend_tax import accrue_dividend_taxes, build_dividend_index
from dividend_lowvol_rotation.rqalpha.bar_price_store import BarPriceStore
from dividend_lowvol_rotation.rqalpha.execution_rules import hold_days_since
from dividend_lowvol_rotation.rqalpha.bridge import RebalancePlan


@dataclass
class ShareOrder:
    code: str
    delta_shares: int
    reason: str = ""


def _trade_price(
    code: str,
    panel: pd.DataFrame,
    as_of: pd.Timestamp,
    side: str,
    store,
    *,
    metrics: dict | None = None,
    trade_amount_cny: float | None = None,
) -> float | None:
    raw = resolve_execution_raw_price(
        code, as_of, store, panel=panel, metrics=metrics
    )
    if raw is None or raw <= 0:
        return None
    if uses_rqalpha_price_source():
        from dividend_lowvol_rotation.rqalpha.rqalpha_bundle_prices import (
            is_suspended_on_date,
        )

        if is_suspended_on_date(code, as_of):
            return None
    vol = metrics.get("ann_vol_pct") if metrics else None
    if vol is None and panel is not None and not panel.empty and "code" in panel.columns:
        row = panel[panel["code"] == code]
        if not row.empty and "ann_vol_pct" in row.columns:
            vol = float(row["ann_vol_pct"].iloc[0])
    amount = trade_amount_cny
    if amount is None and raw:
        amount = raw * 5000
    return trade_execution_price(raw, side, ann_vol_pct=vol, trade_amount_cny=amount)


def accrue_period_dividends(
    lots: dict[str, PositionLot],
    div_index: dict,
    period_start: pd.Timestamp | None,
    period_end: pd.Timestamp,
    *,
    dividend_cash: bool = BACKTEST_DIVIDEND_CASH,
    apply_tax: bool = DIVIDEND_TAX_ENABLED,
) -> float:
    """返回应计入现金池的税后分红（与 backtest._credit_period_dividends 一致）。"""
    if not lots or period_start is None or not dividend_cash:
        return 0.0
    tax, gross, _rows = accrue_dividend_taxes(lots, div_index, period_start, period_end)
    if gross <= 0:
        return 0.0
    return gross - (tax if apply_tax else 0.0)


def build_position_lots(
    holdings: dict[str, int],
    buy_dates: dict[str, pd.Timestamp],
    ctx: BacktestContext,
    panel: pd.DataFrame,
    as_of: pd.Timestamp,
    *,
    price_map: dict[str, float] | None = None,
) -> dict[str, PositionLot]:
    lots: dict[str, PositionLot] = {}
    for code, shares in holdings.items():
        if shares <= 0:
            continue
        price = None
        if price_map and str(code) in price_map:
            price = price_map[str(code)]
        if not price:
            metrics = ctx.store.metrics_at(code, as_of)
            price = metrics.get("price") or ctx.store.price_at(code, as_of) or 0.0
        buy = buy_dates.get(code, as_of)
        lots[code] = PositionLot(
            code=code,
            name="",
            shares=int(shares),
            buy_date=pd.Timestamp(buy).normalize(),
            buy_price=float(price),
            cost_basis=float(shares) * float(price),
            buy_fee=0.0,
            peak_price=float(price),
            prev_price=float(price),
        )
    return lots


def simulate_native_rebalance(
    ctx: BacktestContext,
    plan: RebalancePlan,
    *,
    holdings: dict[str, int],
    buy_dates: dict[str, pd.Timestamp],
    cash: float,
    top_n: int = TOP_N_BUY,
    min_hold_days: int = 0,
    dividend_topup: float = 0.0,
    price_map: dict[str, float] | None = None,
    port_value_override: float | None = None,
    position_scale_override: float | None = None,
    trade_rows: list[dict] | None = None,
    stock_stats: dict[str, StockStats] | None = None,
    name_cache: dict[str, str] | None = None,
    rank_map: dict | None = None,
) -> tuple[dict[str, int], float, list[str], list[ShareOrder], dict[str, PositionLot]]:
    """模拟 _apply_index_dividend_rebalance，返回目标股数、模拟期末现金、备注。"""
    notes: list[str] = []
    if plan.panel is None or plan.ranked is None or plan.buy_pool is None:
        notes.append("缺少 panel/ranked/buy_pool，无法模拟原生再平衡")
        return dict(holdings), cash, notes, [], {}

    panel = plan.panel
    ranked = plan.ranked
    buy_pool = plan.buy_pool
    as_of = plan.as_of
    rb_date = as_of

    lots = build_position_lots(
        holdings, buy_dates, ctx, panel, as_of, price_map=price_map
    )
    sim_cash = float(cash) + float(dividend_topup)
    if dividend_topup > 0:
        notes.append(f"分红计入现金 +{dividend_topup:,.0f}")

    store = BarPriceStore(ctx.store, price_map) if price_map else ctx.store

    def trade_price_fn(code, panel, as_of, side, *, metrics=None, trade_amount_cny=None):
        m = metrics or store.metrics_at(code, as_of)
        return _trade_price(
            code, panel, as_of, side, store,
            metrics=m, trade_amount_cny=trade_amount_cny,
        )

    # 与 backtest.py 一致：port_value 在 index_rules 卖出前计算
    pre_equity = 0.0
    for code, lot in lots.items():
        px = store.price_at(code, rb_date)
        if px and px > 0:
            pre_equity += lot.shares * px
    port_value = float(port_value_override) if port_value_override is not None else (sim_cash + pre_equity)
    position_scale = (
        float(position_scale_override)
        if position_scale_override is not None
        else float(plan.position_scale)
    )

    # index_rules / 调出：先全额卖出（与 backtest.py 调仓主循环一致），再指数再平衡
    share_orders: list[ShareOrder] = []
    sell_set = set(plan.sell_codes or [])
    for code in list(lots.keys()):
        if code not in sell_set:
            continue
        if min_hold_days > 0 and hold_days_since(buy_dates.get(code), as_of) < min_hold_days:
            continue
        lot = lots[code]
        if lot.shares <= 0:
            del lots[code]
            continue
        metrics = store.metrics_at(code, rb_date)
        mkt_price = metrics.get("price") or store.price_at(code, rb_date)
        if not mkt_price or mkt_price <= 0:
            continue
        price = trade_price_fn(
            code,
            panel,
            rb_date,
            "sell",
            metrics=metrics,
            trade_amount_cny=mkt_price * lot.shares,
        )
        if price is None or price <= 0:
            continue
        shares = int(lot.shares)
        proceeds = shares * price
        fee, stamp, net = settle_sell(proceeds, rb_date)
        sim_cash += net
        share_orders.append(
            ShareOrder(code=code, delta_shares=-shares, reason="index_rules或调出")
        )
        del lots[code]

    rank_map = rank_map if rank_map is not None else dict(zip(ranked["code"], ranked["rank"]))
    name_cache = name_cache if name_cache is not None else {}
    stock_stats = stock_stats if stock_stats is not None else {}
    trade_rows = trade_rows if trade_rows is not None else []

    from dividend_lowvol_rotation.backtest import _apply_index_dividend_rebalance

    lots, sim_cash = _apply_index_dividend_rebalance(
        lots=lots,
        cash=sim_cash,
        buy_pool=buy_pool,
        ranked=ranked,
        panel=panel,
        store=store,
        rb_date=rb_date,
        top_n=top_n,
        position_scale=position_scale,
        port_value=port_value,
        rank_map=rank_map,
        name_cache=name_cache,
        stock_stats=stock_stats,
        trade_rows=trade_rows,
        min_hold_days=min_hold_days,
        trade_price_fn=trade_price_fn,
    )

    target_shares = {code: int(lot.shares) for code, lot in lots.items() if lot.shares > 0}
    share_orders.extend(orders_from_trade_rows(trade_rows))
    return target_shares, sim_cash, notes[:8], share_orders, lots


def orders_from_trade_rows(trade_rows: list[dict]) -> list[ShareOrder]:
    """按 backtest 成交顺序生成订单（先卖后买，逐笔扣减现金）。"""
    orders: list[ShareOrder] = []
    for row in trade_rows:
        code = str(row.get("code", ""))
        shares = int(row.get("shares") or 0)
        if not code or shares <= 0:
            continue
        side = str(row.get("side", ""))
        delta = -shares if side == "卖出" else shares
        orders.append(
            ShareOrder(code=code, delta_shares=delta, reason=str(row.get("reason", "")))
        )
    return orders


def sort_share_orders_sell_first(orders: list[ShareOrder]) -> list[ShareOrder]:
    """先卖后买，与实盘结算顺序一致。"""
    sells = [o for o in orders if o.delta_shares < 0]
    buys = [o for o in orders if o.delta_shares > 0]
    return sells + buys


def orders_from_targets(
    target_shares: dict[str, int],
    current_shares: dict[str, int],
    *,
    min_hold_days: int,
    buy_dates: dict[str, pd.Timestamp],
    as_of: pd.Timestamp,
    force_sell_codes: list[str] | None = None,
) -> list[ShareOrder]:
    """当前持仓 → 目标股数，生成整手订单（卖出仍受最短持有期约束）。"""
    force_sell = set(force_sell_codes or [])
    orders: list[ShareOrder] = []
    all_codes = set(current_shares) | set(target_shares)

    for code in sorted(all_codes):
        cur = int(current_shares.get(code, 0))
        tgt = int(target_shares.get(code, 0))
        if code in force_sell:
            tgt = 0
        delta = tgt - cur
        if delta == 0:
            continue
        if delta < 0 and hold_days_since(buy_dates.get(code), as_of) < min_hold_days:
            continue
        orders.append(ShareOrder(code=code, delta_shares=delta, reason="native_rebalance"))
    return orders


def init_dividend_index(ctx: BacktestContext) -> dict:
    return build_dividend_index(ctx.records)
