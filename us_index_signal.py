"""美股指数（纳指 100 / 标普 500）估值信号与报告格式化。"""

from buy_amount_config import enrich_signal_buy_amount
import config
from config import BUY_RANGE_LOOKBACK_DAYS
from drop_to_buy import format_buy_trigger_line, us_index_drop_to_buy
from price_position import (
    build_buy_price_ceilings,
    is_near_year_low,
    make_trend_criterion,
    make_year_range_criterion,
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


def _cfg(key: str, suffix: str):
    return getattr(config, f"{key.upper()}_{suffix}")


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
    forward_pct = snapshot.get("forward_pe_percentile")
    rate_pct = snapshot.get("us10y_percentile")
    rate_slope = snapshot.get("us10y_slope")
    year_range = snapshot.get("year_range_position")
    near_low = is_near_year_low(year_range, _cfg(key, "BUY_NEAR_YEAR_LOW_RANGE_PCT"))

    pe_threshold = _cfg(key, "BUY_FORWARD_PE_PERCENTILE_MAX")
    if near_low:
        pe_threshold = min(100.0, pe_threshold + _cfg(key, "BUY_NEAR_YEAR_LOW_PE_RELAX"))

    pe_ok = forward_pct is not None and forward_pct <= pe_threshold
    pe_label = "Forward PE 分位"
    pe_pct = forward_pct
    display_threshold = pe_threshold

    rate_max = _cfg(key, "BUY_RATE_PERCENTILE_MAX")
    if near_low:
        rate_max = min(100.0, rate_max + _cfg(key, "BUY_NEAR_YEAR_LOW_RATE_RELAX"))
    rate_ok = rate_pct is not None and rate_pct <= rate_max

    slope_max = _cfg(key, "BUY_RATE_MAX_SLOPE")
    slope_lookback = _cfg(key, "BUY_RATE_SLOPE_LOOKBACK_DAYS")
    slope_ok = rate_slope is None or rate_slope <= slope_max
    slope_bps = rate_slope * 10000 if rate_slope is not None else None
    slope_limit_bps = slope_max * 10000

    buy_criteria = [
        make_criterion(
            pe_label,
            pe_ok,
            f"{pct_text(pe_pct)}（需≤{display_threshold:.0f}%）",
            "市盈率处于近10年偏高位",
            applicable=pe_pct is not None,
        ),
        make_criterion(
            "10Y 利率分位",
            rate_ok,
            f"{pct_text(rate_pct)}（需≤{rate_max:.0f}%）",
            "无风险利率偏高，压制估值",
            applicable=rate_pct is not None,
        ),
        make_criterion(
            f"10Y 利率{slope_lookback}日升幅",
            slope_ok,
            (
                f"{slope_bps:+.0f}bp（需≤{slope_limit_bps:.0f}bp）"
                if slope_bps is not None
                else "—"
            ),
            "美债利率短期急升，暂缓买入",
            applicable=rate_slope is not None,
        ),
    ]
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
        and rate_ok
        and slope_ok
        and year_range_ok(year_range, _cfg(key, "BUY_MAX_YEAR_RANGE_PCT"))
        and trend_filter_ok(
            snapshot.get("ma_slope_pct"),
            year_range,
            _cfg(key, "BUY_TREND_MIN_MA_SLOPE_PCT"),
            _cfg(key, "BUY_TREND_DOWNTREND_MAX_RANGE_PCT"),
        )
    )

    signal_short = SIGNAL_BUY if is_buy else SIGNAL_HOLD
    return {
        "is_buy": is_buy,
        "score": sum(1 for c in buy_criteria if c["passed"]),
        "total": len([c for c in buy_criteria if c.get("applicable", True)]),
        "signal_short": signal_short,
        "criteria": buy_criteria,
    }


def is_buy(key: str, snapshot):
    return evaluate_signal(key, snapshot)["is_buy"]


def format_section(key: str, snapshot, signal_eval):
    drop, rise_breaks = us_index_drop_to_buy(key, snapshot)
    price_ceilings = build_buy_price_ceilings(
        snapshot,
        max_above_low_pct=None,
        min_drawdown_pct=None,
        max_year_range_pct=_cfg(key, "BUY_MAX_YEAR_RANGE_PCT"),
        range_lookback_days=BUY_RANGE_LOOKBACK_DAYS,
    )
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
    from live_snapshot import format_live_meta_extra

    trailing_days = snapshot.get("trailing_history_days", 0)
    live_extra = format_live_meta_extra(snapshot)
    meta_extras = [x for x in (f"TTM PE 样本 {trailing_days} 日", live_extra) if x]
    meta_line = format_data_meta_line(
        snapshot.get("data_date") or snapshot.get("date"),
        snapshot.get("history_start"),
        snapshot.get("daily_history_days", snapshot.get("history_days", 0)),
        extras=meta_extras or None,
    )

    lines = [
        f"{snapshot['code']} {snapshot['name']}",
        meta_line,
    ]
    from signal_enrich import build_section_dict, enrich_signal_eval

    signal_eval = enrich_signal_eval(snapshot, signal_eval)
    signal_eval = enrich_signal_buy_amount(snapshot["code"], snapshot, signal_eval)
    append_signal_block(lines, signal_eval, key)
    return build_section_dict(snapshot, signal_eval, lines)


def format_report(key: str, snapshot, section):
    return section["text"], section
