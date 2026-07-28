"""恒生科技指数估值信号与报告格式化（PE + PEG + 股息率分位，无 PB/PS 历史数据源）。"""

import pandas as pd

from config import (
    BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
    BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT,
    BUY_RANGE_LOOKBACK_DAYS,
    HSTECH_BUY_DIV_PERCENTILE_MIN,
    HSTECH_BUY_HIGH_LOOKBACK_DAYS,
    HSTECH_BUY_LOW_LOOKBACK_DAYS,
    HSTECH_BUY_MAX_ABOVE_LOW_PCT,
    HSTECH_BUY_MAX_YEAR_RANGE_PCT,
    HSTECH_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT,
    HSTECH_BUY_MID_RANGE_POSITION_PCT,
    HSTECH_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT,
    HSTECH_BUY_NEAR_YEAR_LOW_PE_RELAX,
    HSTECH_BUY_NEAR_YEAR_LOW_PEG_RELAX,
    HSTECH_BUY_NEAR_YEAR_LOW_DIV_RELAX,
    HSTECH_BUY_NEAR_YEAR_LOW_RANGE_PCT,
    HSTECH_BUY_PE_PERCENTILE_MAX,
    HSTECH_BUY_PEG_HIST_MAX,
    HSTECH_BUY_TREND_DOWNTREND_MAX_RANGE_PCT,
    HSTECH_BUY_TREND_MA_DAYS,
    HSTECH_BUY_TREND_MIN_MA_SLOPE_PCT,
    HSTECH_BUY_TREND_SLOPE_LOOKBACK_DAYS,
    HSTECH_HISTORICAL_GROWTH,
    HSTECH_SELL_ABOVE_LOW_MIN,
    HSTECH_SELL_COST_LOOKBACK_DAYS,
    HSTECH_SELL_ENABLED,
    HSTECH_SELL_PE_PERCENTILE_MIN,
    HSTECH_SELL_PEG_HIST_MIN,
)
from drop_to_buy import (
    format_buy_trigger_line,
    format_drop_to_buy_line,
    format_sell_trigger_line,
    hstech_drop_to_buy,
    hstech_sell_trigger,
)
from price_position import (
    build_buy_price_ceilings,
    drawdown_from_high_ok,
    effective_drawdown_threshold,
    effective_max_above_low_pct,
    format_price_position_line,
    is_near_year_low,
    make_drawdown_from_high_criterion,
    make_price_position_criterion,
    make_trend_criterion,
    make_year_range_criterion,
    price_position_ok,
    trend_filter_ok,
    year_range_ok,
)
from signal_format import (
    SIGNAL_BUY,
    SIGNAL_HOLD,
    SIGNAL_SELL,
    append_signal_block,
    format_module_header,
    make_criterion,
    pct_text,
)


def compute_peg(pe, growth_rate):
    if pe is None or growth_rate is None or growth_rate <= 0:
        return None
    return pe / (growth_rate * 100)


def _hstech_row_snapshot(row):
    """从估值面板行构建 evaluate_hstech_signal 所需字段。"""
    return {
        "pe": row.get("pe"),
        "pe_percentile": row.get("pe_percentile"),
        "dividend_percentile": row.get("dividend_percentile"),
        "pct_above_low": row.get("pct_above_low"),
        "pct_below_high": row.get("pct_below_high"),
        "year_range_position": row.get("year_range_position"),
        "ma_slope_pct": row.get("ma_slope_pct"),
        "close": row.get("close"),
    }


def compute_recent_signal_buy_avg(
    panel,
    lookback_days=None,
    date_col="date",
    close_col="close",
):
    """近 N 个交易日内，策略买入信号日的收盘价算术平均（报告用隐含成本）。"""
    if panel is None or panel.empty or close_col not in panel.columns:
        return None
    lookback = lookback_days or HSTECH_SELL_COST_LOOKBACK_DAYS
    work = panel.tail(lookback)
    prices = []
    for _, row in work.iterrows():
        if evaluate_hstech_signal(_hstech_row_snapshot(row))["is_buy"]:
            close = row.get(close_col)
            if close is not None and not pd.isna(close):
                prices.append(float(close))
    if not prices:
        return None
    return sum(prices) / len(prices)


def valuation_sell_triggered(snapshot):
    """估值层面的卖出触发：PE 分位偏高，且 PEG 过高或距低点涨幅过大。"""
    pe = snapshot.get("pe")
    peg_historical = compute_peg(pe, HSTECH_HISTORICAL_GROWTH)
    pe_pct = snapshot.get("pe_percentile")
    above_low = snapshot.get("pct_above_low")

    pe_high = pe_pct is not None and pe_pct >= HSTECH_SELL_PE_PERCENTILE_MIN
    peg_high = peg_historical is not None and peg_historical >= HSTECH_SELL_PEG_HIST_MIN
    momentum_high = (
        above_low is not None and above_low >= HSTECH_SELL_ABOVE_LOW_MIN
    )

    triggered = pe_high and (peg_high or momentum_high)
    reasons = []
    if triggered:
        reasons.append(f"PE分位{pct_text(pe_pct)}偏高")
        if peg_high:
            reasons.append(f"PEG(5年){peg_historical:.2f}偏高")
        if momentum_high:
            pct = above_low * 100 if above_low is not None else None
            reasons.append(
                f"距近1年低点涨幅{pct_text(pct) if pct is not None else '—'}"
            )

    return {
        "triggered": triggered,
        "reasons": reasons,
        "pe_high": pe_high,
        "peg_high": peg_high,
        "momentum_high": momentum_high,
    }


def evaluate_hstech_signal(snapshot):
    """PE 历史分位 + 保守 PEG（近5年增速）+ 股息率分位；乐咕暂无 PB/PS 历史。"""
    pe = snapshot.get("pe")
    peg_historical = compute_peg(pe, HSTECH_HISTORICAL_GROWTH)
    pe_pct = snapshot.get("pe_percentile")
    div_pct = snapshot.get("dividend_percentile")
    year_range = snapshot.get("year_range_position")
    near_low = is_near_year_low(year_range, HSTECH_BUY_NEAR_YEAR_LOW_RANGE_PCT)
    pe_max = HSTECH_BUY_PE_PERCENTILE_MAX
    peg_max = HSTECH_BUY_PEG_HIST_MAX
    div_min = HSTECH_BUY_DIV_PERCENTILE_MIN
    if near_low:
        pe_max = min(100.0, pe_max + HSTECH_BUY_NEAR_YEAR_LOW_PE_RELAX)
        peg_max += HSTECH_BUY_NEAR_YEAR_LOW_PEG_RELAX
        div_min = max(0.0, div_min - HSTECH_BUY_NEAR_YEAR_LOW_DIV_RELAX)
    max_above_low = effective_max_above_low_pct(
        HSTECH_BUY_MAX_ABOVE_LOW_PCT,
        year_range,
        HSTECH_BUY_NEAR_YEAR_LOW_RANGE_PCT,
        BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
        HSTECH_BUY_MID_RANGE_POSITION_PCT,
        HSTECH_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT,
    )
    min_drawdown = effective_drawdown_threshold(
        HSTECH_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT,
        year_range,
        BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT,
    )

    pe_ok = pe_pct is not None and pe_pct <= pe_max
    peg_hist_ok = (
        peg_historical is not None and peg_historical <= peg_max
    )
    div_ok = (
        div_pct is not None and div_pct >= div_min
    )

    buy_criteria = [
        make_criterion(
            "PE 分位",
            pe_ok,
            f"{pct_text(pe_pct)}（需≤{pe_max:.0f}%）",
            "市盈率处于历史中高位",
            applicable=pe_pct is not None,
        ),
        make_criterion(
            "PEG(近5年增速)",
            peg_hist_ok,
            (
                f"{peg_historical:.2f}（需≤{peg_max:.1f}）"
                if peg_historical is not None
                else "—"
            ),
            "估值相对历史盈利增速偏贵",
            applicable=peg_historical is not None,
        ),
        make_criterion(
            "股息率分位",
            div_ok,
            f"{pct_text(div_pct)}（需≥{div_min:.0f}%）",
            "股息率相对历史偏低，估值吸引力不足",
            applicable=div_pct is not None,
        ),
    ]
    price_criterion = make_price_position_criterion(
        snapshot.get("pct_above_low"),
        max_above_low,
        HSTECH_BUY_LOW_LOOKBACK_DAYS,
        close=snapshot.get("close"),
        lookback_low=snapshot.get("lookback_low_price"),
    )
    if price_criterion is not None:
        buy_criteria.append(price_criterion)
    drawdown_criterion = make_drawdown_from_high_criterion(
        snapshot.get("pct_below_high"),
        min_drawdown,
        HSTECH_BUY_HIGH_LOOKBACK_DAYS,
        close=snapshot.get("close"),
        lookback_high=snapshot.get("lookback_high_price"),
    )
    if drawdown_criterion is not None:
        buy_criteria.append(drawdown_criterion)
    year_range_criterion = make_year_range_criterion(
        year_range,
        HSTECH_BUY_MAX_YEAR_RANGE_PCT,
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
        HSTECH_BUY_TREND_MIN_MA_SLOPE_PCT,
        HSTECH_BUY_TREND_DOWNTREND_MAX_RANGE_PCT,
        HSTECH_BUY_TREND_MA_DAYS,
        HSTECH_BUY_TREND_SLOPE_LOOKBACK_DAYS,
    )
    if trend_criterion is not None:
        buy_criteria.append(trend_criterion)

    criteria = [c for c in buy_criteria if c["applicable"]]
    score = sum(1 for c in criteria if c["passed"])
    is_buy = (
        pe_ok
        and peg_hist_ok
        and div_ok
        and price_position_ok(
            snapshot.get("pct_above_low"), max_above_low
        )
        and drawdown_from_high_ok(snapshot.get("pct_below_high"), min_drawdown)
        and year_range_ok(year_range, HSTECH_BUY_MAX_YEAR_RANGE_PCT)
        and trend_filter_ok(
            snapshot.get("ma_slope_pct"),
            year_range,
            HSTECH_BUY_TREND_MIN_MA_SLOPE_PCT,
            HSTECH_BUY_TREND_DOWNTREND_MAX_RANGE_PCT,
        )
    )

    is_sell = False
    sell_reasons = []
    val_sell = {"triggered": False, "reasons": []}
    if HSTECH_SELL_ENABLED:
        val_sell = valuation_sell_triggered(snapshot)
        if val_sell["triggered"]:
            is_sell = True
            sell_reasons.extend(val_sell["reasons"])

    if is_buy:
        signal_short = SIGNAL_BUY
        summary = "PE 处历史低位，PEG(5年) 可接受，且股息率相对历史偏高"
    elif is_sell:
        signal_short = SIGNAL_SELL
        summary = "触发波段卖出: " + "；".join(sell_reasons)
    else:
        signal_short = SIGNAL_HOLD
        failed = [c["name"] for c in criteria if not c["passed"]]
        summary = f"未达标项: {'、'.join(failed)}" if failed else "指标接近但未同时满足买入条件"

    return {
        "peg_historical": peg_historical,
        "historical_growth": HSTECH_HISTORICAL_GROWTH,
        "is_buy": is_buy,
        "is_sell": is_sell,
        "valuation_sell": val_sell["triggered"],
        "score": score,
        "signal_short": signal_short,
        "criteria": criteria,
        "summary": summary,
    }


def format_hstech_section(snapshot, signal_eval):
    year_range = snapshot.get("year_range_position")
    near_low = is_near_year_low(year_range, HSTECH_BUY_NEAR_YEAR_LOW_RANGE_PCT)
    max_above_low = effective_max_above_low_pct(
        HSTECH_BUY_MAX_ABOVE_LOW_PCT,
        year_range,
        HSTECH_BUY_NEAR_YEAR_LOW_RANGE_PCT,
        BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
        HSTECH_BUY_MID_RANGE_POSITION_PCT,
        HSTECH_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT,
    )
    min_drawdown = effective_drawdown_threshold(
        HSTECH_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT,
        year_range,
        BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT,
    )
    price_ceilings = build_buy_price_ceilings(
        snapshot,
        max_above_low,
        min_drawdown,
        HSTECH_BUY_MAX_YEAR_RANGE_PCT,
        low_lookback_days=HSTECH_BUY_LOW_LOOKBACK_DAYS,
        high_lookback_days=HSTECH_BUY_HIGH_LOOKBACK_DAYS,
        range_lookback_days=BUY_RANGE_LOOKBACK_DAYS,
    )
    drop, rise_breaks = hstech_drop_to_buy(snapshot)
    drop_breaks = (
        hstech_sell_trigger(snapshot) if signal_eval.get("is_sell") else None
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
    peg_hist = signal_eval.get("peg_historical")
    peg_hist_text = f"{peg_hist:.2f}" if peg_hist is not None else "—"
    div_yield = snapshot.get("dividend_yield")

    lines = [
        f"{snapshot['code']} {snapshot['name']}",
        (
            f"PE {snapshot['pe']:.2f} | PE分位 {pct_text(snapshot.get('pe_percentile'))} | "
            f"股息率 {div_yield * 100:.2f}% | 股息率分位 {pct_text(snapshot.get('dividend_percentile'))}"
        ),
        f"PEG(5年) {peg_hist_text} | 近5年增速 {signal_eval['historical_growth']:.1%}",
    ]
    lines.append(format_price_position_line(snapshot, HSTECH_BUY_LOW_LOOKBACK_DAYS))
    append_signal_block(lines, signal_eval, "hstech")
    return {
        "code": snapshot["code"],
        "name": snapshot["name"],
        "text": "\n".join(lines),
        "signal_short": signal_eval["signal_short"],
    }


def format_hstech_report(snapshot, section):
    hist_days = snapshot.get("history_days", 0)
    lines = format_module_header(
        "恒生科技指数 估值信号",
        f"{snapshot['date']} | 历史样本约 {hist_days} 个交易日 | 主口径: 乐咕恒生科技 PE/股息率（月度发布并按指数价日度折算）",
        "买入逻辑: PE 分位偏低 + PEG(5年)≤阈值 + 股息率分位偏高 + 价格位置（须同时满足）；"
        "卖出: 无（长期持有）",
    )
    lines.append(section["text"])
    return "\n".join(lines), section
