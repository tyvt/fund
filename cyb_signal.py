"""创业板指估值信号与报告格式化（加权 PE/PB 为主）。"""

from buy_amount_config import enrich_signal_buy_amount
from config import (
    BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
    BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT,
    BUY_RANGE_LOOKBACK_DAYS,
    CYB_BUY_HIGH_LOOKBACK_DAYS,
    CYB_BUY_LOW_LOOKBACK_DAYS,
    CYB_BUY_MAX_ABOVE_LOW_PCT,
    CYB_BUY_MAX_YEAR_RANGE_PCT,
    CYB_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT,
    CYB_BUY_MID_RANGE_POSITION_PCT,
    CYB_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT,
    CYB_BUY_NEAR_YEAR_LOW_PE_RELAX,
    CYB_BUY_NEAR_YEAR_LOW_RANGE_PCT,
    CYB_BUY_PB_PERCENTILE_MAX,
    CYB_BUY_PE_PERCENTILE_MAX,
    CYB_BUY_PEG_HIST_MAX,
    CYB_BUY_TREND_DOWNTREND_MAX_RANGE_PCT,
    CYB_BUY_TREND_MA_DAYS,
    CYB_BUY_TREND_MIN_MA_SLOPE_PCT,
    CYB_BUY_TREND_SLOPE_LOOKBACK_DAYS,
    CYB_HISTORICAL_GROWTH,
    CYB_SELL_COMBO_PB_PERCENTILE_MIN,
    CYB_SELL_COMBO_PE_PERCENTILE_MIN,
    CYB_SELL_PB_PERCENTILE_MIN,
    CYB_SELL_PE_PERCENTILE_MIN,
    CYB_SELL_PEG_HIST_MIN,
    CYB_SELL_ENABLED,
    CYB_SELL_MIN_UNREALIZED_GAIN_PCT,
    CYB_SELL_TRAILING_DRAWDOWN_PCT,
    CYB_SELL_TRAILING_MIN_HOLD_DAYS,
)
from drop_to_buy import (
    cyb_drop_to_buy,
    cyb_sell_trigger,
    format_buy_trigger_line,
    format_sell_trigger_line,
)
from price_position import (
    build_buy_price_ceilings,
    drawdown_from_high_ok,
    effective_drawdown_threshold,
    effective_max_above_low_pct,
    is_near_year_low,
    make_drawdown_from_high_criterion,
    make_price_position_criterion,
    make_trend_criterion,
    make_year_range_criterion,
    price_position_ok,
    trend_filter_ok,
    year_range_ok,
)
from sell_trailing import trailing_sell_hit
from signal_format import (
    SIGNAL_BUY,
    SIGNAL_HOLD,
    SIGNAL_SELL,
    append_signal_block,
    format_data_meta_line,
    make_criterion,
    pct_text,
)


def compute_peg(pe, growth_rate):
    """PEG = PE / 净利润增速（百分比）。"""
    if pe is None or growth_rate is None or growth_rate <= 0:
        return None
    return pe / (growth_rate * 100)


def evaluate_cyb_signal(snapshot):
    """加权估值为主：PE/PB 历史分位 + 保守 PEG（近5年增速）。"""
    pe = snapshot.get("pe")
    peg_historical = compute_peg(pe, CYB_HISTORICAL_GROWTH)

    pe_pct = snapshot.get("pe_percentile")
    pb_pct = snapshot.get("pb_percentile")
    year_range = snapshot.get("year_range_position")
    near_low = is_near_year_low(year_range, CYB_BUY_NEAR_YEAR_LOW_RANGE_PCT)
    pe_max = CYB_BUY_PE_PERCENTILE_MAX
    pb_max = CYB_BUY_PB_PERCENTILE_MAX
    if near_low:
        pe_max = min(100.0, pe_max + CYB_BUY_NEAR_YEAR_LOW_PE_RELAX)
        pb_max = min(100.0, pb_max + CYB_BUY_NEAR_YEAR_LOW_PE_RELAX)
    max_above_low = effective_max_above_low_pct(
        CYB_BUY_MAX_ABOVE_LOW_PCT,
        year_range,
        CYB_BUY_NEAR_YEAR_LOW_RANGE_PCT,
        BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
        CYB_BUY_MID_RANGE_POSITION_PCT,
        CYB_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT,
    )
    min_drawdown = effective_drawdown_threshold(
        CYB_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT,
        year_range,
        BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT,
    )

    pe_ok = pe_pct is not None and pe_pct <= pe_max
    pb_ok = pb_pct is not None and pb_pct <= pb_max
    peg_hist_ok = peg_historical is not None and peg_historical <= CYB_BUY_PEG_HIST_MAX

    buy_criteria = [
        make_criterion(
            "PE 分位(加权)",
            pe_ok,
            f"{pct_text(pe_pct)}（需≤{pe_max:.0f}%）",
            "市盈率处于历史中高位",
            applicable=pe_pct is not None,
        ),
        make_criterion(
            "PB 分位(加权)",
            pb_ok,
            f"{pct_text(pb_pct)}（需≤{pb_max:.0f}%）",
            "市净率处于历史中高位",
            applicable=pb_pct is not None,
        ),
        make_criterion(
            "PEG(近5年增速)",
            peg_hist_ok,
            (
                f"{peg_historical:.2f}（需≤{CYB_BUY_PEG_HIST_MAX:.1f}）"
                if peg_historical is not None
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
    drawdown_criterion = make_drawdown_from_high_criterion(
        snapshot.get("pct_below_high"),
        min_drawdown,
        CYB_BUY_HIGH_LOOKBACK_DAYS,
        close=snapshot.get("close"),
        lookback_high=snapshot.get("lookback_high_price"),
    )
    if drawdown_criterion is not None:
        buy_criteria.append(drawdown_criterion)
    year_range_criterion = make_year_range_criterion(
        year_range,
        CYB_BUY_MAX_YEAR_RANGE_PCT,
        BUY_RANGE_LOOKBACK_DAYS,
        close=snapshot.get("close"),
        range_low=snapshot.get("range_low_price"),
        range_high=snapshot.get("range_high_price"),
    )
    if year_range_criterion is not None:
        buy_criteria.append(year_range_criterion)
    trend_criterion = make_trend_criterion(
        snapshot.get("ma_slope_pct"),
        year_range,
        CYB_BUY_TREND_MIN_MA_SLOPE_PCT,
        CYB_BUY_TREND_DOWNTREND_MAX_RANGE_PCT,
        CYB_BUY_TREND_MA_DAYS,
        CYB_BUY_TREND_SLOPE_LOOKBACK_DAYS,
    )
    if trend_criterion is not None:
        buy_criteria.append(trend_criterion)

    criteria = [c for c in buy_criteria if c["applicable"]]
    score = sum(1 for c in criteria if c["passed"])
    is_buy = (
        pe_ok
        and pb_ok
        and peg_hist_ok
        and price_position_ok(snapshot.get("pct_above_low"), max_above_low)
        and drawdown_from_high_ok(snapshot.get("pct_below_high"), min_drawdown)
        and year_range_ok(year_range, CYB_BUY_MAX_YEAR_RANGE_PCT)
        and trend_filter_ok(
            snapshot.get("ma_slope_pct"),
            year_range,
            CYB_BUY_TREND_MIN_MA_SLOPE_PCT,
            CYB_BUY_TREND_DOWNTREND_MAX_RANGE_PCT,
        )
    )

    is_sell = False
    sell_reasons = []
    if CYB_SELL_ENABLED:
        close = snapshot.get("close")
        recent_avg = snapshot.get("recent_signal_buy_avg")
        peak_price = snapshot.get("peak_since_last_buy")
        days_since_buy = snapshot.get("days_since_last_buy")
        trail_hit = trailing_sell_hit(
            close=close,
            cost_basis=recent_avg,
            peak_price=peak_price,
            min_unrealized_gain_pct=CYB_SELL_MIN_UNREALIZED_GAIN_PCT,
            trailing_drawdown_pct=CYB_SELL_TRAILING_DRAWDOWN_PCT,
            min_hold_days=CYB_SELL_TRAILING_MIN_HOLD_DAYS,
            days_since_buy=days_since_buy,
        )
        pe_high = pe_pct is not None and pe_pct >= CYB_SELL_PE_PERCENTILE_MIN
        pb_high = pb_pct is not None and pb_pct >= CYB_SELL_PB_PERCENTILE_MIN
        peg_combo = (
            peg_historical is not None
            and peg_historical >= CYB_SELL_PEG_HIST_MIN
            and pe_pct is not None
            and pb_pct is not None
            and pe_pct >= CYB_SELL_COMBO_PE_PERCENTILE_MIN
            and pb_pct >= CYB_SELL_COMBO_PB_PERCENTILE_MIN
        )
        if trail_hit:
            is_sell = True
            sell_reasons.append("移动止盈触发")
        elif pe_high and pb_high:
            is_sell = True
            sell_reasons.append(f"PE分位{pct_text(pe_pct)}、PB分位{pct_text(pb_pct)}均偏高")
        elif peg_combo:
            is_sell = True
            sell_reasons.append(f"PEG(5年){peg_historical:.2f}偏高且估值不低")

    if is_buy:
        signal_short = SIGNAL_BUY
        summary = "加权 PE/PB 处历史低位，且 PEG(5年) 可接受"
    elif is_sell:
        signal_short = SIGNAL_SELL
        summary = "触发波段卖出: " + "；".join(sell_reasons)
    else:
        signal_short = SIGNAL_HOLD
        failed = [c["name"] for c in criteria if not c["passed"]]
        summary = f"未达标项: {'、'.join(failed)}" if failed else "指标接近但未同时满足买入条件"

    return {
        "peg_historical": peg_historical,
        "historical_growth": CYB_HISTORICAL_GROWTH,
        "is_buy": is_buy,
        "is_sell": is_sell,
        "score": score,
        "signal_short": signal_short,
        "criteria": criteria,
        "summary": summary,
    }


def format_cyb_section(snapshot, signal_eval):
    year_range = snapshot.get("year_range_position")
    near_low = is_near_year_low(year_range, CYB_BUY_NEAR_YEAR_LOW_RANGE_PCT)
    max_above_low = effective_max_above_low_pct(
        CYB_BUY_MAX_ABOVE_LOW_PCT,
        year_range,
        CYB_BUY_NEAR_YEAR_LOW_RANGE_PCT,
        BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
        CYB_BUY_MID_RANGE_POSITION_PCT,
        CYB_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT,
    )
    min_drawdown = effective_drawdown_threshold(
        CYB_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT,
        year_range,
        BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT,
    )
    price_ceilings = build_buy_price_ceilings(
        snapshot,
        max_above_low,
        min_drawdown,
        CYB_BUY_MAX_YEAR_RANGE_PCT,
        low_lookback_days=CYB_BUY_LOW_LOOKBACK_DAYS,
        high_lookback_days=CYB_BUY_HIGH_LOOKBACK_DAYS,
        range_lookback_days=BUY_RANGE_LOOKBACK_DAYS,
    )
    drop, rise_breaks = cyb_drop_to_buy(snapshot)
    drop_breaks = (
        cyb_sell_trigger(snapshot) if signal_eval.get("is_sell") else None
    )
    buy_line = format_buy_trigger_line(
        drop,
        is_buy=signal_eval.get("is_buy"),
        rise_breaks_pct=rise_breaks,
        close=snapshot.get("close"),
        price_ceilings=price_ceilings,
    )
    sell_line = format_sell_trigger_line(
        is_sell=signal_eval.get("is_sell"),
        drop_breaks_pct=drop_breaks,
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
    return {
        "code": snapshot["code"],
        "name": snapshot["name"],
        "text": "\n".join(lines),
        "signal_short": signal_eval["signal_short"],
    }


def format_cyb_report(snapshot, section):
    return section["text"], section
