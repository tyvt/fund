"""交易成本估算（双边佣金 + 最低 5 元）。"""

from __future__ import annotations

from datetime import date

from dividend_lowvol_rotation.config import (
    BACKTEST_INITIAL_CAPITAL,
    COMMISSION_RATE,
    ESTIMATE_TURNOVER_FRACTION,
    LOT_SIZE,
    MIN_COMMISSION_CNY,
    PORTFOLIO_CAPITAL_CNY,
)

# 与 RQAlpha sys_transaction_cost 一致（2023-08-28 起减半）
STAMP_TAX_CHANGE_DATE = date(2023, 8, 28)


def uses_live_settlement() -> bool:
    """是否按 RQAlpha / A 股实盘口径结算（印花税 + 分红税时点）。"""
    from dividend_lowvol_rotation.config import uses_rqalpha_execution_model

    return uses_rqalpha_execution_model()


def stamp_tax_rate(as_of) -> float:
    if not uses_live_settlement():
        return 0.0
    import pandas as pd

    day = pd.Timestamp(as_of).normalize()
    if day < pd.Timestamp(STAMP_TAX_CHANGE_DATE):
        return 0.001
    return 0.0005


def sell_stamp_tax(proceeds: float, as_of) -> float:
    if proceeds <= 0:
        return 0.0
    return proceeds * stamp_tax_rate(as_of)


def settle_sell(proceeds: float, as_of) -> tuple[float, float, float]:
    """卖出结算：佣金 + 印花税（卖侧），返回 (fee, stamp_tax, net_cash)。"""
    fee = single_side_commission(proceeds)
    stamp = sell_stamp_tax(proceeds, as_of)
    return fee, stamp, proceeds - fee - stamp


def dynamic_slippage_rate(
    *,
    ann_vol_pct: float | None = None,
    trade_amount_cny: float | None = None,
) -> float:
    from dividend_lowvol_rotation.config import (
        EXECUTION_AT_CLOSE,
        SLIPPAGE_ADV_BASE_CNY,
        SLIPPAGE_BASE_RATE,
        SLIPPAGE_DYNAMIC_ENABLED,
        SLIPPAGE_MAX_RATE,
        SLIPPAGE_PARTICIPATION_MULT,
        SLIPPAGE_RATE,
        uses_rqalpha_execution_model,
    )

    if EXECUTION_AT_CLOSE:
        return 0.0
    # RQAlpha PriceRatioSlippage 为固定比例；动态滑点仅用于非 rqalpha 模式的保守估算
    if not SLIPPAGE_DYNAMIC_ENABLED or uses_rqalpha_execution_model():
        return SLIPPAGE_RATE
    vol = ann_vol_pct if ann_vol_pct is not None and ann_vol_pct > 0 else 25.0
    adv = SLIPPAGE_ADV_BASE_CNY * (22.0 / max(vol, 12.0))
    amount = trade_amount_cny or 0.0
    participation = amount / max(adv, 1.0)
    rate = SLIPPAGE_BASE_RATE + participation * SLIPPAGE_PARTICIPATION_MULT
    return min(SLIPPAGE_MAX_RATE, max(SLIPPAGE_BASE_RATE, rate))


def apply_slippage(
    price: float,
    side: str,
    *,
    ann_vol_pct: float | None = None,
    trade_amount_cny: float | None = None,
    slippage_rate: float | None = None,
) -> float:
    rate = slippage_rate
    if rate is None:
        rate = dynamic_slippage_rate(ann_vol_pct=ann_vol_pct, trade_amount_cny=trade_amount_cny)
    if rate <= 0 or price <= 0:
        return price
    if side == "buy":
        return price * (1 + rate)
    return price * (1 - rate)


def trade_execution_price(
    price: float,
    side: str,
    *,
    ann_vol_pct: float | None = None,
    trade_amount_cny: float | None = None,
) -> float:
    """调仓成交价：默认收盘价；``DLV_EXECUTION_AT_CLOSE=false`` 时叠加滑点。"""
    if price <= 0:
        return price
    from dividend_lowvol_rotation.config import EXECUTION_AT_CLOSE

    if EXECUTION_AT_CLOSE:
        return price
    return apply_slippage(
        price,
        side,
        ann_vol_pct=ann_vol_pct,
        trade_amount_cny=trade_amount_cny,
    )


def resolve_execution_raw_price(
    code: str,
    as_of: pd.Timestamp,
    store,
    *,
    panel: pd.DataFrame | None = None,
    metrics: dict | None = None,
) -> float | None:
    """调仓用价：rqalpha 模式下强制 store 收盘价（与 RQ bar.close / 实盘同源）。"""
    from dividend_lowvol_rotation.config import EXECUTION_AT_CLOSE, uses_rqalpha_price_source

    if uses_rqalpha_price_source() and EXECUTION_AT_CLOSE and store is not None:
        px = store.price_at(code, as_of)
        if px and px > 0:
            return float(px)
    if metrics and metrics.get("price"):
        px = float(metrics["price"])
        if px > 0:
            return px
    if panel is not None and not panel.empty and "code" in panel.columns:
        row = panel[panel["code"] == code]
        if not row.empty and "price" in row.columns:
            px = float(row["price"].iloc[0])
            if px > 0:
                return px
    if store is not None:
        return store.price_at(code, as_of)
    return None


def single_side_commission(trade_amount_cny: float) -> float:
    if trade_amount_cny <= 0:
        return 0.0
    return max(trade_amount_cny * COMMISSION_RATE, MIN_COMMISSION_CNY)


def floor_lot_shares(amount_cny: float, price: float, lot_size: int = LOT_SIZE) -> int:
    """按预算向下取整到整手股数。"""
    if price <= 0 or amount_cny <= 0 or lot_size <= 0:
        return 0
    return int(amount_cny / price // lot_size) * lot_size


def buy_order_cost(shares: int, price: float) -> tuple[float, float, float]:
    """返回 (成交额, 佣金, 总支出)。"""
    gross = shares * price
    fee = single_side_commission(gross)
    return gross, fee, gross + fee


def max_buy_shares(budget_cny: float, price: float, lot_size: int = LOT_SIZE) -> int:
    """在预算内可买入的最大整手股数（含佣金）。"""
    shares = floor_lot_shares(budget_cny, price, lot_size)
    while shares >= lot_size:
        _, _, total = buy_order_cost(shares, price)
        if total <= budget_cny + 1e-6:
            return shares
        shares -= lot_size
    return 0


def round_trip_commission(trade_amount_cny: float) -> float:
    return single_side_commission(trade_amount_cny) * 2


def estimate_rebalance_cost(
    capital_cny: float,
    position_count: int,
    *,
    turnover_fraction: float = ESTIMATE_TURNOVER_FRACTION,
) -> dict:
    """估算单次轮动成本：假设 turnover_fraction 的仓位发生买卖。"""
    if capital_cny <= 0 or position_count <= 0:
        return {
            "capital_cny": capital_cny,
            "per_position_cny": None,
            "trades_estimated": 0,
            "one_side_commission_cny": None,
            "round_trip_commission_cny": None,
            "total_rebalance_cost_cny": None,
            "cost_pct_of_capital": None,
        }
    per_position = capital_cny / position_count
    one_side = single_side_commission(per_position)
    round_trip = one_side * 2
    # 换手：卖出旧仓 + 买入新仓，约 2 × turnover_fraction × n 笔单边
    trades = max(1, int(round(position_count * turnover_fraction * 2)))
    total = trades * one_side
    return {
        "capital_cny": capital_cny,
        "per_position_cny": per_position,
        "trades_estimated": trades,
        "one_side_commission_cny": one_side,
        "round_trip_commission_cny": round_trip,
        "total_rebalance_cost_cny": total,
        "cost_pct_of_capital": total / capital_cny * 100,
    }


def resolve_report_capital(capital_cny: float | None = None) -> float:
    """报告用资金：CLI > 环境变量 > 回测默认初始资金。"""
    if capital_cny is not None and capital_cny > 0:
        return float(capital_cny)
    if PORTFOLIO_CAPITAL_CNY > 0:
        return float(PORTFOLIO_CAPITAL_CNY)
    return float(BACKTEST_INITIAL_CAPITAL)


def plan_portfolio_allocation(
    portfolio_df,
    capital_cny: float,
    *,
    lot_size: int = LOT_SIZE,
) -> tuple[object, dict]:
    """按目标权重估算整手买入计划（含单边佣金）。"""
    empty_summary = {
        "capital_cny": capital_cny,
        "total_invested_cny": 0.0,
        "total_commission_cny": 0.0,
        "cash_remaining_cny": capital_cny,
        "utilization_pct": 0.0,
    }
    if portfolio_df is None or getattr(portfolio_df, "empty", True) or capital_cny <= 0:
        return portfolio_df, empty_summary

    out = portfolio_df.copy()
    shares_list: list[int] = []
    amount_list: list[float] = []
    fee_list: list[float] = []
    total_cost_list: list[float] = []
    total_invested = 0.0
    total_fee = 0.0

    n = len(out)
    for _, row in out.iterrows():
        weight_pct = row.get("target_weight_pct")
        if weight_pct is None or (isinstance(weight_pct, float) and weight_pct != weight_pct):
            weight = 1.0 / n if n else 0.0
        else:
            weight = float(weight_pct) / 100.0
        price = float(row.get("price") or 0)
        if price <= 0 or weight <= 0:
            shares_list.append(0)
            amount_list.append(0.0)
            fee_list.append(0.0)
            total_cost_list.append(0.0)
            continue
        budget = capital_cny * weight
        shares = max_buy_shares(budget, price, lot_size)
        gross, fee, total = buy_order_cost(shares, price) if shares > 0 else (0.0, 0.0, 0.0)
        shares_list.append(shares)
        amount_list.append(gross)
        fee_list.append(fee)
        total_cost_list.append(total)
        total_invested += total

    out["buy_shares"] = shares_list
    out["buy_amount_cny"] = amount_list
    out["buy_commission_cny"] = fee_list
    out["buy_total_cost_cny"] = total_cost_list
    total_fee = sum(fee_list)
    cash_remaining = max(0.0, capital_cny - total_invested)
    summary = {
        "capital_cny": capital_cny,
        "total_invested_cny": total_invested,
        "total_commission_cny": total_fee,
        "cash_remaining_cny": cash_remaining,
        "utilization_pct": total_invested / capital_cny * 100 if capital_cny > 0 else 0.0,
    }
    return out, summary


def format_cost_note(capital_cny: float | None, position_count: int) -> str | None:
    cap = resolve_report_capital(capital_cny)
    if cap <= 0:
        return None
    est = estimate_rebalance_cost(cap, position_count)
    return (
        f"资金 **{cap:,.0f}** 元、持仓 **{position_count}** 只："
        f"单笔约 **{est['per_position_cny']:,.0f}** 元，"
        f"单边佣金 **{est['one_side_commission_cny']:.2f}** 元（万 {COMMISSION_RATE*10000:.3f}、"
        f"最低 {MIN_COMMISSION_CNY:.0f} 元），"
        f"估算单次轮动 **{est['trades_estimated']}** 笔单边、合计约 **{est['total_rebalance_cost_cny']:.0f}** 元"
        f"（占资金 {est['cost_pct_of_capital']:.2f}%）"
    )
