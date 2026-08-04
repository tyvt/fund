"""买入金额：基准配置、涨跌缩放与报告展示。"""

from __future__ import annotations

from buy_amount_change import (
    format_change_amount_line,
    make_change_amount_fn,
    resolve_change_scaled_amount,
    row_daily_change_pct,
)

ALL_BUY_INDEX_CODES = (
    "H30269",
    "000852",
    "000688",
    "399006",
    "NDX",
    "SPX",
)


def _change_scale_enabled(amounts) -> bool:
    from config import BUY_AMOUNT_CHANGE_SCALE_ENABLED

    if amounts is not None and "change_scale" in amounts:
        return bool(amounts.get("change_scale"))
    return BUY_AMOUNT_CHANGE_SCALE_ENABLED


def resolve_simulate_amount(
    code,
    base_amt,
    amounts,
    panel,
    start_date,
    end_date,
    buy_fn,
    date_col="date",
):
    """回测用：固定金额，或基准 × 当日涨跌系数。"""
    from buy_amount_budget import is_annual_budget_enabled, make_annual_amount_fn

    if base_amt <= 0:
        return 0

    reference_base = base_amt
    if is_annual_budget_enabled(amounts):
        by_ref = amounts.get("reference_by_code") if amounts else None
        if by_ref and code in by_ref:
            reference_base = float(by_ref[code])
        return make_annual_amount_fn(
            code,
            reference_base,
            amounts,
            panel,
            start_date,
            end_date,
            buy_fn,
            date_col,
        )

    if not _change_scale_enabled(amounts):
        return base_amt
    return make_change_amount_fn(
        base_amt, panel, date_col=date_col, close_col="close"
    )


def format_live_buy_amount_line(snapshot, base: float) -> str:
    """实时价下展示买入金额（含涨跌缩放说明）。"""
    from config import BUY_AMOUNT_CHANGE_SCALE_ENABLED
    from price_position import format_index_price

    if BUY_AMOUNT_CHANGE_SCALE_ENABLED:
        return format_change_amount_line(snapshot, base)
    close = snapshot.get("close")
    prev = snapshot.get("close_prev")
    delta_pct = snapshot.get("live_price_delta_pct")
    price_part = f"当前 {format_index_price(close)}"
    if (
        prev is not None
        and close is not None
        and delta_pct is not None
        and abs(float(close) - float(prev)) > 1e-6
    ):
        price_part += (
            f"（昨收 {format_index_price(prev)}，{delta_pct * 100:+.2f}%）"
        )
    return f"{price_part} **{base:.0f}元**"


def enrich_signal_buy_amount(index_code, snapshot, signal_eval):
    """为信号评估附加买入金额，供报告展示（仅触发买入时）。"""
    from config import (
        BUY_AMOUNT_CHANGE_SCALE_ENABLED,
        get_buy_amount_base,
        is_index_recommended,
    )

    out = dict(signal_eval)

    if not out.get("is_buy"):
        return out

    recommended = is_index_recommended(index_code)
    base = get_buy_amount_base(index_code)
    mult = float(out.get("cooldown_amount_multiplier") or 1.0)
    effective_base = base * mult
    out["recommended"] = recommended
    out["buy_amount_base"] = base
    out["buy_amount_effective"] = effective_base

    if base <= 0:
        return out

    mult_hint = f"（频次补偿×{mult:.1f}）" if mult > 1.01 else ""

    if BUY_AMOUNT_CHANGE_SCALE_ENABLED:
        amt = resolve_change_scaled_amount(effective_base, snapshot)
        out["buy_amount"] = amt
        chg = row_daily_change_pct(snapshot)
        out["buy_amount_line"] = (
            f"买入金额: {format_change_amount_line(snapshot, effective_base)}"
            f"{mult_hint}"
        )
        if chg is not None:
            out["daily_change_pct"] = chg
    elif snapshot.get("live_price"):
        out["buy_amount"] = effective_base
        out["buy_amount_line"] = (
            f"买入金额: {format_live_buy_amount_line(snapshot, effective_base)}"
            f"{mult_hint}"
        )
    else:
        out["buy_amount"] = effective_base
        out["buy_amount_line"] = f"买入金额: **{effective_base:.0f} 元**{mult_hint}"

    return out
