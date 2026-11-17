"""创业板指估值信号与报告格式化（PEG + 价格位置 + 趋势）。"""

from buy_amount_config import enrich_signal_buy_amount
from config import (
    BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
    BUY_RANGE_LOOKBACK_DAYS,
    CYB_BUY_LOW_LOOKBACK_DAYS,
    CYB_BUY_MAX_ABOVE_LOW_PCT,
    CYB_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT,
    CYB_BUY_MID_RANGE_POSITION_PCT,
    CYB_BUY_NEAR_YEAR_LOW_RANGE_PCT,
    CYB_BUY_PEG_HIST_MAX,
    CYB_SELL_ENABLED,
    CYB_SELL_STAGES,
    SELL_REBUY_GATE_ENABLED,
    SELL_REBUY_MAX_GAIN_PCT,
)
from drop_to_buy import (
    cyb_drop_to_buy,
    format_buy_trigger_line,
    format_sell_trigger_line,
)
from price_position import (
    build_buy_price_ceilings,
    effective_max_above_low_pct,
    make_price_position_criterion,
    price_position_ok,
)
from sell_trailing import rebuy_allowed_after_take_profit, trailing_sell_hit
from signal_format import (
    SIGNAL_BUY,
    SIGNAL_HOLD,
    SIGNAL_SELL,
    append_signal_block,
    format_data_meta_line,
    make_criterion,
)


def compute_peg(pe, growth_rate):
    """PEG = PE / 净利润增速（百分比）。"""
    if pe is None or growth_rate is None or growth_rate <= 0:
        return None
    return pe / (growth_rate * 100)


def _snapshot_first(snapshot, *names):
    """按优先级取快照字段，兼容实盘与回测两套键名。"""
    for name in names:
        value = snapshot.get(name)
        if value is not None:
            return value
    return None


def cyb_valuation_sell_decision(snapshot):
    """创业板卖出判定：单一粗移动止盈（A 方案）。

    浮盈≥50% 且自峰值回撤≥15% 且持有≥90日。旧 PE/PB 高分位、PEG 高卖出
    逻辑已删除。

    返回 ``(is_sell, sell_reasons)``。
    """
    if not CYB_SELL_ENABLED:
        return False, []

    trail_hit = trailing_sell_hit(
        close=snapshot.get("close"),
        cost_basis=_snapshot_first(snapshot, "cost_basis", "recent_signal_buy_avg"),
        peak_price=_snapshot_first(snapshot, "peak_price", "peak_since_last_buy"),
        min_unrealized_gain_pct=0.50,
        trailing_drawdown_pct=0.15,
        min_hold_days=90,
        days_since_buy=_snapshot_first(
            snapshot, "days_since_buy", "days_since_last_buy"
        ),
    )
    reasons = ["移动止盈触发"] if trail_hit else []
    return bool(reasons), reasons


def cyb_valuation_sell_fn(snapshot):
    """回测估值卖点函数：``snapshot(row) -> is_sell``。

    直接传给 ``simulate_trades_trailing(valuation_sell_fn=...)``；
    row 为面板行字典，缺失字段不会误触发。若需原因请用
    :func:`cyb_valuation_sell_decision`。
    """
    return cyb_valuation_sell_decision(snapshot)[0]


def evaluate_cyb_signal(snapshot):
    """买入：PEG(近5年增速) + 价格位置 + MA 趋势。"""
    from cyb_data import resolve_cyb_historical_growth

    pe = snapshot.get("pe")
    hist_growth = resolve_cyb_historical_growth(
        panel=snapshot.get("panel"), snapshot=snapshot
    )
    peg_historical = compute_peg(pe, hist_growth)

    year_range = snapshot.get("year_range_position")
    max_above_low = effective_max_above_low_pct(
        CYB_BUY_MAX_ABOVE_LOW_PCT,
        year_range,
        CYB_BUY_NEAR_YEAR_LOW_RANGE_PCT,
        BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
        CYB_BUY_MID_RANGE_POSITION_PCT,
        CYB_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT,
    )

    peg_hist_ok = peg_historical is not None and peg_historical <= CYB_BUY_PEG_HIST_MAX

    buy_criteria = [
        make_criterion(
            "PEG(近5年增速)",
            peg_hist_ok,
            (
                f"{peg_historical:.2f}（需≤{CYB_BUY_PEG_HIST_MAX:.1f}，"
                f"增速 {hist_growth * 100:.1f}%）"
                if peg_historical is not None and hist_growth is not None
                else "—"
            ),
            "估值相对历史盈利增速偏贵",
            applicable=peg_historical is not None,
        ),
    ]
    price_criterion = make_price_position_criterion(
        snapshot.get("pct_above_low"),
        max_above_low,
        CYB_BUY_LOW_LOOKBACK_DAYS,
        close=snapshot.get("close"),
        lookback_low=snapshot.get("lookback_low_price"),
    )
    if price_criterion is not None:
        buy_criteria.append(price_criterion)

    criteria = [c for c in buy_criteria if c["applicable"]]
    score = sum(1 for c in criteria if c["passed"])
    is_buy = (
        peg_hist_ok
        and price_position_ok(snapshot.get("pct_above_low"), max_above_low)
    )

    if is_buy and not rebuy_allowed_after_take_profit(
        close=snapshot.get("close"),
        cost_basis=snapshot.get("recent_signal_buy_avg"),
        peak_price=snapshot.get("peak_since_last_buy"),
        max_gain_pct=SELL_REBUY_MAX_GAIN_PCT,
        first_stage_gain_pct=(
            float(CYB_SELL_STAGES[0]["gain_pct"]) if CYB_SELL_STAGES else 0.50
        ),
        gate_enabled=SELL_REBUY_GATE_ENABLED,
    ):
        is_buy = False

    is_sell, sell_reasons = cyb_valuation_sell_decision(
        {**snapshot, "peg_historical": peg_historical}
    )

    if is_buy:
        signal_short = SIGNAL_BUY
        summary = "PEG(5年) 可接受，且价格/趋势达标"
    elif is_sell:
        signal_short = SIGNAL_SELL
        summary = "触发波段卖出: " + "；".join(sell_reasons)
    else:
        signal_short = SIGNAL_HOLD
        failed = [c["name"] for c in criteria if not c["passed"]]
        summary = f"未达标项: {'、'.join(failed)}" if failed else "指标接近但未同时满足买入条件"

    return {
        "peg_historical": peg_historical,
        "historical_growth": hist_growth,
        "is_buy": is_buy,
        "is_sell": is_sell,
        "score": score,
        "signal_short": signal_short,
        "criteria": criteria,
        "summary": summary,
    }


def format_cyb_section(snapshot, signal_eval):
    year_range = snapshot.get("year_range_position")
    max_above_low = effective_max_above_low_pct(
        CYB_BUY_MAX_ABOVE_LOW_PCT,
        year_range,
        CYB_BUY_NEAR_YEAR_LOW_RANGE_PCT,
        BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
        CYB_BUY_MID_RANGE_POSITION_PCT,
        CYB_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT,
    )
    price_ceilings = build_buy_price_ceilings(
        snapshot,
        max_above_low,
        min_drawdown_pct=None,
        max_year_range_pct=None,
        low_lookback_days=CYB_BUY_LOW_LOOKBACK_DAYS,
        high_lookback_days=None,
        range_lookback_days=BUY_RANGE_LOOKBACK_DAYS,
    )
    drop, rise_breaks = cyb_drop_to_buy(snapshot)
    buy_line = format_buy_trigger_line(
        drop,
        is_buy=signal_eval.get("is_buy"),
        rise_breaks_pct=rise_breaks,
        close=snapshot.get("close"),
        price_ceilings=price_ceilings,
    )
    sell_line = format_sell_trigger_line(
        is_sell=signal_eval.get("is_sell"),
        drop_breaks_pct=None,
        close=snapshot.get("close"),
    )
    signal_eval = {
        **signal_eval,
        "drop_to_buy": drop,
        "rise_breaks_buy": rise_breaks,
        "drop_to_buy_line": buy_line,
        "buy_trigger_line": buy_line,
        "sell_trigger_line": sell_line,
    }
    from signal_enrich import build_section_dict, enrich_signal_eval

    signal_eval = enrich_signal_eval(snapshot, signal_eval)
    from live_snapshot import format_live_meta_extra

    live_extra = format_live_meta_extra(snapshot)
    meta_line = format_data_meta_line(
        snapshot.get("data_date") or snapshot.get("date"),
        snapshot.get("history_start"),
        snapshot.get("history_days"),
        extras=[live_extra] if live_extra else None,
    )

    lines = [
        f"{snapshot['code']} {snapshot['name']}",
        meta_line,
    ]
    signal_eval = enrich_signal_buy_amount(snapshot["code"], snapshot, signal_eval)
    append_signal_block(lines, signal_eval, "cyb")
    return build_section_dict(snapshot, signal_eval, lines)


def format_cyb_report(snapshot, section):
    return section["text"], section
