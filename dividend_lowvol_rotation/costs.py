"""交易成本估算（双边佣金 + 最低 5 元）。"""

from __future__ import annotations

from dividend_lowvol_rotation.config import (
    COMMISSION_RATE,
    ESTIMATE_TURNOVER_FRACTION,
    LOT_SIZE,
    MIN_COMMISSION_CNY,
    PORTFOLIO_CAPITAL_CNY,
)


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


def format_cost_note(capital_cny: float | None, position_count: int) -> str | None:
    cap = capital_cny if capital_cny and capital_cny > 0 else PORTFOLIO_CAPITAL_CNY
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
