"""买入金额：基准配置、分档计算与报告展示。"""

from __future__ import annotations

from buy_amount_tiers import (
    get_tier_scheme,
    resolve_tiered_amount,
    row_price_position,
)

ALL_BUY_INDEX_CODES = (
    "930955",
    "H30269",
    "000510",
    "000300",
    "000905",
    "000852",
    "000688",
    "399006",
    "HSTECH",
    "NDX",
    "SPX",
)


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
    """回测用：固定金额、按年预算动态金额或分档 amount_fn。"""
    from buy_amount_budget import is_annual_budget_enabled, make_annual_amount_fn
    from buy_amount_tiers import estimate_avg_multiplier, make_amount_fn

    if base_amt <= 0:
        return 0

    reference_base = base_amt
    if is_annual_budget_enabled(amounts):
        by_ref = amounts.get("reference_by_code") if amounts else None
        if by_ref and code in by_ref:
            reference_base = float(by_ref[code])

    if is_annual_budget_enabled(amounts):
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

    if not amounts or not amounts.get("tier_scheme"):
        return base_amt
    scale = 1.0
    by_code = amounts.get("tier_norm_by_code")
    if by_code and code in by_code:
        scale = float(by_code[code])
    elif amounts.get("tier_normalize"):
        avg = estimate_avg_multiplier(
            panel, start_date, end_date, buy_fn, amounts["tier_scheme"], date_col
        )
        scale = 1.0 / avg if avg > 0 else 1.0
    return make_amount_fn(base_amt, amounts["tier_scheme"], scale=scale)


def _amount_at_position(snapshot, base, scheme, position: float) -> float:
    row = dict(snapshot)
    row["year_range_position"] = max(0.0, min(1.0, position))
    return resolve_tiered_amount(base, row, scheme)


def _price_at_position(range_low: float, span: float, position: float) -> float:
    return range_low + position * span


def format_buy_amount_scenarios(snapshot, base: float, scheme: str) -> str | None:
    """按昨日区间高低点，列出平开/涨跌分档临界价位的买入金额。"""
    from price_position import format_index_price

    close = snapshot.get("close")
    if close is None:
        return None

    range_low = snapshot.get("range_low_price")
    range_high = snapshot.get("range_high_price")
    current_pos = row_price_position(snapshot)
    amt_now = resolve_tiered_amount(base, snapshot, scheme)

    parts = [f"平开（昨收 {format_index_price(close)}）**{amt_now:.0f}元**"]

    if (
        range_low is None
        or range_high is None
        or range_high <= range_low
        or current_pos is None
    ):
        return "；".join(parts)

    span = range_high - range_low
    tier_scheme = get_tier_scheme(scheme)
    boundaries = [max_pos for max_pos, _ in tier_scheme.tiers]

    drop_items = []
    lower_bounds = sorted([pos for pos in boundaries if pos < current_pos - 1e-6])
    for drop_pos in reversed(lower_bounds):
        drop_price = _price_at_position(range_low, span, drop_pos)
        drop_amt = _amount_at_position(snapshot, base, scheme, drop_pos)
        drop_items.append((drop_price, drop_amt))
    if current_pos > 1e-6:
        drop_items.append((range_low, _amount_at_position(snapshot, base, scheme, 0.0)))

    seen_drop_amts = {amt_now}
    for drop_price, drop_amt in sorted(drop_items, key=lambda item: item[0], reverse=True):
        if drop_amt in seen_drop_amts:
            continue
        seen_drop_amts.add(drop_amt)
        parts.append(f"跌至 {format_index_price(drop_price)} **{drop_amt:.0f}元**")

    rise_items = []
    upper_bounds = sorted([pos for pos in boundaries if pos > current_pos + 1e-6])
    for rise_pos in upper_bounds:
        rise_price = _price_at_position(range_low, span, rise_pos)
        rise_amt = _amount_at_position(snapshot, base, scheme, min(1.0, rise_pos + 1e-4))
        rise_items.append((rise_price, rise_amt))

    seen_rise_amts = {amt_now}
    for rise_price, rise_amt in sorted(rise_items, key=lambda item: item[0]):
        if rise_amt in seen_rise_amts:
            continue
        seen_rise_amts.add(rise_amt)
        parts.append(f"涨至 {format_index_price(rise_price)} **{rise_amt:.0f}元**")

    return "；".join(parts)


BUY_REFERENCE_MAX_TRIGGER_GAP = 0.10


def _show_buy_amount_line(signal_eval) -> bool:
    """未触发买入时，触发跌幅超过阈值则不展示买入参考。"""
    if signal_eval.get("is_buy"):
        return True
    drop = signal_eval.get("drop_to_buy")
    if drop is None:
        return False
    return drop <= BUY_REFERENCE_MAX_TRIGGER_GAP


def enrich_signal_buy_amount(index_code, snapshot, signal_eval):
    """为信号评估附加基准/分档买入金额，供报告展示。"""
    from config import BUY_AMOUNT_TIER_ENABLED, BUY_AMOUNT_TIER_SCHEME, get_buy_amount_base

    base = get_buy_amount_base(index_code)
    if base <= 0:
        return signal_eval

    out = dict(signal_eval)
    out["buy_amount_base"] = base

    if not _show_buy_amount_line(out):
        return out

    if BUY_AMOUNT_TIER_ENABLED:
        scenario_line = format_buy_amount_scenarios(snapshot, base, BUY_AMOUNT_TIER_SCHEME)
        if scenario_line:
            label = "买入金额" if out.get("is_buy") else "买入参考"
            out["buy_amount_line"] = f"{label}: {scenario_line}"
    elif out.get("is_buy"):
        out["buy_amount_line"] = f"买入金额: **{base:.0f} 元**"
    else:
        out["buy_amount_line"] = f"买入参考: **{base:.0f} 元**"

    return out
