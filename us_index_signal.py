"""美股指数（纳指 100 / 标普 500）估值信号与报告格式化。"""

from buy_amount_config import enrich_signal_buy_amount
import config
from config import BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX, BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT, BUY_RANGE_LOOKBACK_DAYS
from drop_to_buy import format_buy_trigger_line, us_index_drop_to_buy
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
    append_signal_block,
    format_data_meta_line,
    make_criterion,
    pct_text,
)

DIVIDEND_LABELS = {
    "ndx": "股息率(QQQ代理)",
    "spx": "股息率(SPY代理)",
}


def _cfg(key: str, suffix: str):
    return getattr(config, f"{key.upper()}_{suffix}")


def compute_peg(pe, growth_rate):
    if pe is None or growth_rate is None or growth_rate <= 0:
        return None
    return pe / (growth_rate * 100)


def resolve_expected_growth(key: str, snapshot):
    explicit = snapshot.get("expected_growth")
    if explicit is not None:
        return explicit
    configured = _cfg(key, "EXPECTED_GROWTH")
    if configured is not None:
        return configured
    implied = snapshot.get("implied_growth")
    if implied is not None and implied > 0:
        return implied
    historical = snapshot.get("historical_growth")
    if historical is not None and historical > 0:
        return historical
    return _cfg(key, "FALLBACK_EXPECTED_GROWTH")


def evaluate_signal(key: str, snapshot):
    expected_growth = resolve_expected_growth(key, snapshot)

    trailing_pe = snapshot.get("trailing_pe")
    forward_pe = snapshot.get("forward_pe")
    peg_forward = compute_peg(forward_pe, expected_growth)
    peg_hist = compute_peg(trailing_pe, snapshot.get("historical_growth"))

    trailing_pct = snapshot.get("trailing_pe_percentile")
    forward_pct = snapshot.get("forward_pe_percentile")
    rate_pct = snapshot.get("us10y_percentile")
    year_range = snapshot.get("year_range_position")
    near_low = is_near_year_low(year_range, _cfg(key, "BUY_NEAR_YEAR_LOW_RANGE_PCT"))
    pe_threshold = _cfg(key, "BUY_FORWARD_PE_PERCENTILE_MAX")
    trailing_threshold = _cfg(key, "BUY_TRAILING_PE_PERCENTILE_MAX")
    if near_low:
        pe_threshold = min(100.0, pe_threshold + _cfg(key, "BUY_NEAR_YEAR_LOW_PE_RELAX"))
        trailing_threshold = min(
            100.0, trailing_threshold + _cfg(key, "BUY_NEAR_YEAR_LOW_PE_RELAX")
        )
    max_above_low = effective_max_above_low_pct(
        _cfg(key, "BUY_MAX_ABOVE_LOW_PCT"),
        year_range,
        _cfg(key, "BUY_NEAR_YEAR_LOW_RANGE_PCT"),
        BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
        _cfg(key, "BUY_MID_RANGE_POSITION_PCT"),
        _cfg(key, "BUY_MID_RANGE_MAX_ABOVE_LOW_PCT"),
    )
    min_drawdown = effective_drawdown_threshold(
        _cfg(key, "BUY_MIN_DRAWDOWN_FROM_HIGH_PCT"),
        year_range,
        BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT,
    )

    trailing_ok = trailing_pct is not None and trailing_pct <= trailing_threshold
    forward_ok = forward_pct is not None and forward_pct <= pe_threshold
    if forward_pct is not None:
        pe_ok = forward_ok
        pe_label = "Forward PE 分位"
        pe_pct = forward_pct
        display_threshold = pe_threshold
    elif trailing_pct is not None:
        pe_ok = trailing_ok
        pe_label = "TTM PE 分位"
        pe_pct = trailing_pct
        display_threshold = trailing_threshold
    else:
        pe_ok = False
        pe_label = "PE 分位"
        pe_pct = None
        display_threshold = pe_threshold

    peg_forward_max = _cfg(key, "BUY_PEG_FORWARD_MAX")
    if expected_growth is not None and expected_growth >= _cfg(key, "HIGH_GROWTH_THRESHOLD"):
        peg_forward_max += _cfg(key, "HIGH_GROWTH_PEG_BONUS")
    if near_low:
        peg_forward_max += _cfg(key, "BUY_NEAR_YEAR_LOW_PEG_RELAX")

    peg_fwd_ok = peg_forward is not None and peg_forward <= peg_forward_max
    rate_max = _cfg(key, "BUY_RATE_PERCENTILE_MAX")
    if near_low:
        rate_max = min(100.0, rate_max + _cfg(key, "BUY_NEAR_YEAR_LOW_RATE_RELAX"))
    rate_ok = rate_pct is not None and rate_pct <= rate_max

    buy_criteria = [
        make_criterion(
            pe_label,
            pe_ok,
            f"{pct_text(pe_pct)}（需≤{display_threshold:.0f}%）",
            "市盈率处于近10年偏高位",
            applicable=pe_pct is not None,
        ),
        make_criterion(
            "PEG(Forward)",
            peg_fwd_ok,
            (
                f"{peg_forward:.2f}（需≤{peg_forward_max:.1f}）"
                if peg_forward is not None
                else "—"
            ),
            "估值相对预期盈利增速偏贵",
            applicable=peg_forward is not None,
        ),
        make_criterion(
            "10Y 利率分位",
            rate_ok,
            f"{pct_text(rate_pct)}（需≤{rate_max:.0f}%）",
            "无风险利率偏高，压制估值",
            applicable=rate_pct is not None,
        ),
    ]
    price_criterion = make_price_position_criterion(
        snapshot.get("pct_above_low"),
        max_above_low,
        _cfg(key, "BUY_LOW_LOOKBACK_DAYS"),
        close=snapshot.get("close"),
        lookback_low=snapshot.get("lookback_low_price"),
    )
    if price_criterion is not None:
        buy_criteria.append(price_criterion)
    drawdown_criterion = make_drawdown_from_high_criterion(
        snapshot.get("pct_below_high"),
        min_drawdown,
        _cfg(key, "BUY_HIGH_LOOKBACK_DAYS"),
        close=snapshot.get("close"),
        lookback_high=snapshot.get("lookback_high_price"),
    )
    if drawdown_criterion is not None:
        buy_criteria.append(drawdown_criterion)
    year_range_criterion = make_year_range_criterion(
        year_range,
        _cfg(key, "BUY_MAX_YEAR_RANGE_PCT"),
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
        _cfg(key, "BUY_TREND_MIN_MA_SLOPE_PCT"),
        _cfg(key, "BUY_TREND_DOWNTREND_MAX_RANGE_PCT"),
        _cfg(key, "BUY_TREND_MA_DAYS"),
        _cfg(key, "BUY_TREND_SLOPE_LOOKBACK_DAYS"),
    )
    if trend_criterion is not None:
        buy_criteria.append(trend_criterion)

    is_buy = (
        pe_ok
        and peg_fwd_ok
        and rate_ok
        and price_position_ok(snapshot.get("pct_above_low"), max_above_low)
        and drawdown_from_high_ok(snapshot.get("pct_below_high"), min_drawdown)
        and year_range_ok(year_range, _cfg(key, "BUY_MAX_YEAR_RANGE_PCT"))
        and trend_filter_ok(
            snapshot.get("ma_slope_pct"),
            year_range,
            _cfg(key, "BUY_TREND_MIN_MA_SLOPE_PCT"),
            _cfg(key, "BUY_TREND_DOWNTREND_MAX_RANGE_PCT"),
        )
    )

    if is_buy:
        signal_short = SIGNAL_BUY
        summary = "估值分位偏低、PEG 合理且利率环境友好"
    else:
        signal_short = SIGNAL_HOLD
        failed = [c["name"] for c in buy_criteria if c["applicable"] and not c["passed"]]
        summary = f"未达标项: {'、'.join(failed)}" if failed else "指标接近但未同时满足买入条件"

    return {
        "peg_forward": peg_forward,
        "peg_historical": peg_hist,
        "expected_growth": expected_growth,
        "historical_growth": snapshot.get("historical_growth"),
        "implied_growth": snapshot.get("implied_growth"),
        "is_buy": is_buy,
        "score": sum(1 for c in buy_criteria if c["passed"]),
        "signal_short": signal_short,
        "criteria": buy_criteria,
        "summary": summary,
    }


def is_buy(key: str, snapshot):
    return evaluate_signal(key, snapshot)["is_buy"]


def format_section(key: str, snapshot, signal_eval):
    year_range = snapshot.get("year_range_position")
    near_low = is_near_year_low(year_range, _cfg(key, "BUY_NEAR_YEAR_LOW_RANGE_PCT"))
    max_above_low = effective_max_above_low_pct(
        _cfg(key, "BUY_MAX_ABOVE_LOW_PCT"),
        year_range,
        _cfg(key, "BUY_NEAR_YEAR_LOW_RANGE_PCT"),
        BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
        _cfg(key, "BUY_MID_RANGE_POSITION_PCT"),
        _cfg(key, "BUY_MID_RANGE_MAX_ABOVE_LOW_PCT"),
    )
    min_drawdown = effective_drawdown_threshold(
        _cfg(key, "BUY_MIN_DRAWDOWN_FROM_HIGH_PCT"),
        year_range,
        BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT,
    )
    price_ceilings = build_buy_price_ceilings(
        snapshot,
        max_above_low,
        min_drawdown,
        _cfg(key, "BUY_MAX_YEAR_RANGE_PCT"),
        low_lookback_days=_cfg(key, "BUY_LOW_LOOKBACK_DAYS"),
        high_lookback_days=_cfg(key, "BUY_HIGH_LOOKBACK_DAYS"),
        range_lookback_days=BUY_RANGE_LOOKBACK_DAYS,
    )
    drop, rise_breaks = us_index_drop_to_buy(key, snapshot)
    buy_line = format_buy_trigger_line(
        drop,
        is_buy=signal_eval.get("is_buy"),
        rise_breaks_pct=rise_breaks,
        close=snapshot.get("close"),
        price_ceilings=price_ceilings,
    )
    signal_eval = {
        **signal_eval,
        "drop_to_buy": drop,
        "rise_breaks_buy": rise_breaks,
        "drop_to_buy_line": buy_line,
        "buy_trigger_line": buy_line,
    }
    peg_fwd = signal_eval.get("peg_forward")
    peg_fwd_text = f"{peg_fwd:.2f}" if peg_fwd is not None else "—"
    div = snapshot.get("dividend_yield")
    div_text = f"{div:.2%}" if div is not None else "—"
    us10y = snapshot.get("us10y")
    us10y_text = f"{us10y:.2%}" if us10y is not None else "—"
    trailing_days = snapshot.get("trailing_history_days", 0)
    meta_line = format_data_meta_line(
        snapshot.get("data_date") or snapshot.get("date"),
        snapshot.get("history_start"),
        snapshot.get("daily_history_days", snapshot.get("history_days", 0)),
        extras=[f"TTM PE 样本 {trailing_days} 日"],
    )

    lines = [
        f"{snapshot['code']} {snapshot['name']}",
        meta_line,
        (
            f"Forward PE {snapshot.get('forward_pe', 0):.2f} "
            f"(分位 {pct_text(snapshot.get('forward_pe_percentile'))}) | "
            f"PEG(Forward) {peg_fwd_text}"
        ),
    ]
    if signal_eval.get("implied_growth") is not None:
        lines.append(f"隐含增速 {signal_eval['implied_growth']:.1%}")
    lines.append(
        f"10Y美债 {us10y_text} | 利率分位 {pct_text(snapshot.get('us10y_percentile'))} | "
        f"{DIVIDEND_LABELS[key]} {div_text}"
    )
    lines.append(format_price_position_line(snapshot, _cfg(key, "BUY_LOW_LOOKBACK_DAYS")))
    signal_eval = enrich_signal_buy_amount(snapshot["code"], snapshot, signal_eval)
    append_signal_block(lines, signal_eval, key)
    return {
        "code": snapshot["code"],
        "name": snapshot["name"],
        "text": "\n".join(lines),
        "signal_short": signal_eval["signal_short"],
    }


def format_report(key: str, snapshot, section):
    return section["text"], section
