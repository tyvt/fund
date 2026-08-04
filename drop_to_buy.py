"""估算指数价格再跌多少可触发买入（基于昨日估值面板推演，供盘中参考）。"""

from config import (
    BUY_RANGE_LOOKBACK_DAYS,
    CYB_BUY_HIGH_LOOKBACK_DAYS,
    CYB_BUY_LOW_LOOKBACK_DAYS,
    CYB_BUY_TREND_MA_DAYS,
    CYB_BUY_TREND_SLOPE_LOOKBACK_DAYS,
    CYB_PERCENTILE_MIN_DAYS,
    CYB_PERCENTILE_WINDOW,
    DIVIDEND_SPREAD_PERCENTILE_MIN_DAYS,
    DIVIDEND_SPREAD_PERCENTILE_WINDOW,
    get_cn_broad_signal_config,
)
from market_data import compute_percentile
from price_position import (
    ma_slope_for_simulated_close,
    pct_above_low_for_simulated_close,
    pct_below_high_for_simulated_close,
    range_position_for_simulated_close,
    format_index_price,
    format_price_bound_summary,
)


def rolling_percentile(series, idx, value, window, min_days):
    """与回测一致：当前值相对 [idx-window, idx) 历史窗口的分位。"""
    if value is None or idx < min_days:
        return None
    start = max(0, idx - window)
    hist = series.iloc[start:idx]
    if hist is None or hist.empty:
        return None
    return compute_percentile(hist, value)


def find_min_drop_to_buy(is_buy_at_drop, max_drop=0.35, precision=0.002):
    """二分搜索最小跌幅 drop∈[0,1)，使买入条件成立。"""
    try:
        if is_buy_at_drop(0.0):
            return 0.0
        if not is_buy_at_drop(max_drop):
            return None
        lo, hi = 0.0, max_drop
        while hi - lo > precision:
            mid = (lo + hi) / 2.0
            if is_buy_at_drop(mid):
                hi = mid
            else:
                lo = mid
        return hi
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def find_min_rise_breaks_buy(is_buy_at_rise, max_rise=0.35, precision=0.002):
    """昨日已满足买入时，二分搜索当日最小涨幅使买入不再成立。"""
    try:
        if not is_buy_at_rise(0.0):
            return None
        if is_buy_at_rise(max_rise):
            return None
        lo, hi = 0.0, max_rise
        while hi - lo > precision:
            mid = (lo + hi) / 2.0
            if is_buy_at_rise(mid):
                lo = mid
            else:
                hi = mid
        return hi
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def find_min_drop_breaks_condition(
    still_true_at_drop, max_drop=0.35, precision=0.002
):
    """条件已满足时，二分搜索最小跌幅使条件不再成立。"""
    try:
        if not still_true_at_drop(0.0):
            return None
        if still_true_at_drop(max_drop):
            return None
        lo, hi = 0.0, max_drop
        while hi - lo > precision:
            mid = (lo + hi) / 2.0
            if still_true_at_drop(mid):
                lo = mid
            else:
                hi = mid
        return lo
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def format_buy_trigger_line(
    drop_pct=None,
    is_buy=False,
    rise_breaks_pct=None,
    close=None,
    price_ceilings=None,
    max_drop=0.35,
):
    """报告用买入触发文案：未满足时估算达标点位，已满足时标注维持区间。"""
    if is_buy or drop_pct == 0:
        parts = ["触发估算: 已满足买入条件"]
        if close is not None:
            parts[0] += f"（当前 {format_index_price(close)}）"
        hold_parts = []
        if price_ceilings:
            summary = format_price_bound_summary(price_ceilings, "上限")
            if summary:
                hold_parts.append(f"维持买入 {summary}")
        if rise_breaks_pct is not None and close is not None:
            break_price = close * (1 + rise_breaks_pct)
            hold_parts.append(
                f"涨至 {format_index_price(break_price)} 将不再满足买入"
            )
        elif rise_breaks_pct is not None:
            hold_parts.append(
                f"涨幅超 {rise_breaks_pct * 100:.1f}% 将不再满足买入"
            )
        if hold_parts:
            parts.append("；".join(hold_parts))
        elif not price_ceilings:
            parts.append("估值条件较宽，大涨后仍可能维持买入信号")
        return "；".join(parts)

    if drop_pct is None:
        if close is not None:
            floor_price = close * (1 - max_drop)
            return (
                f"触发估算: 跌至约 {format_index_price(floor_price)} 以内仍难达买入标准"
                f"（当前 {format_index_price(close)}）"
            )
        return f"触发估算: 再跌约 {max_drop * 100:.0f}% 以内仍难达买入标准"
    if close is not None:
        target = close * (1 - drop_pct)
        return (
            f"触发估算: 跌至约 {format_index_price(target)} 可达买入标准"
            f"（当前 {format_index_price(close)}，估值推演，盘中参考）"
        )
    return (
        f"触发估算: 指数再跌约 {drop_pct * 100:.1f}% 可达买入标准"
        "（估值推演，盘中参考）"
    )


def format_sell_trigger_line(
    is_sell=False,
    drop_breaks_pct=None,
    close=None,
    price_floors=None,
    max_drop=0.35,
):
    """报告用卖出触发文案：已满足时标注维持区间与失效点位。"""
    if not is_sell:
        return None
    parts = ["触发估算: 已满足卖出条件"]
    if close is not None:
        parts[0] += f"（当前 {format_index_price(close)}）"
    hold_parts = []
    if price_floors:
        summary = format_price_bound_summary(price_floors, "下限")
        if summary:
            hold_parts.append(f"维持卖出 {summary}")
    if drop_breaks_pct is not None and close is not None:
        break_price = close * (1 - drop_breaks_pct)
        hold_parts.append(f"跌至 {format_index_price(break_price)} 将不再满足卖出")
    elif drop_breaks_pct is not None:
        hold_parts.append(f"跌幅超 {drop_breaks_pct * 100:.1f}% 将不再满足卖出")
    if hold_parts:
        parts.append("；".join(hold_parts))
    elif not price_floors:
        parts.append("估值仍偏高，大跌后仍可能维持卖出信号")
    return "；".join(parts)


def format_drop_to_buy_line(
    drop_pct, is_buy=False, max_drop=0.35, rise_breaks_pct=None, close=None,
    price_ceilings=None,
):
    """报告用单行文案（兼容旧调用，内部转 format_buy_trigger_line）。"""
    return format_buy_trigger_line(
        drop_pct=drop_pct,
        is_buy=is_buy,
        rise_breaks_pct=rise_breaks_pct,
        close=close,
        price_ceilings=price_ceilings,
        max_drop=max_drop,
    )


def _estimate_buy_trigger(check_factor, max_move=0.35, precision=0.002):
    """返回 (再跌多少可买入, 已买入时当日涨多少将失效)。"""
    drop = find_min_drop_to_buy(
        lambda d: check_factor(1.0 - d), max_drop=max_move, precision=precision
    )
    rise_breaks = find_min_rise_breaks_buy(
        lambda r: check_factor(1.0 + r), max_rise=max_move, precision=precision
    )
    return drop, rise_breaks


def dividend_drop_to_buy(index_code, bond_history=None, panel=None):
    from config import get_dividend_signal_config
    from dividend_data import build_signal_history, is_buy_signal

    if panel is None:
        panel = build_signal_history(index_code, bond_history=bond_history)
    if panel is None or panel.empty:
        return None

    idx = len(panel) - 1
    row = panel.iloc[-1]
    if row["pe"] is None or row["dividend_yield"] is None or row["bond_yield"] is None:
        return None

    cfg = get_dividend_signal_config(index_code)
    lookback = cfg.get("buy_low_lookback_days", 60)
    high_lookback = cfg.get("buy_high_lookback_days", 252)
    max_above_low = cfg.get("buy_max_above_low_pct")
    min_drawdown = cfg.get("buy_min_drawdown_from_high_pct")

    def check_factor(factor):
        if factor <= 0:
            return False
        pe = row["pe"] * factor
        div = row["dividend_yield"] / factor
        spread = div - row["bond_yield"]
        pe_pct = rolling_percentile(
            panel["pe"],
            idx,
            pe,
            DIVIDEND_SPREAD_PERCENTILE_WINDOW,
            DIVIDEND_SPREAD_PERCENTILE_MIN_DAYS,
        )
        spread_pct = rolling_percentile(
            panel["spread"],
            idx,
            spread,
            DIVIDEND_SPREAD_PERCENTILE_WINDOW,
            DIVIDEND_SPREAD_PERCENTILE_MIN_DAYS,
        )
        pct_above_low = None
        pct_below_high = None
        if max_above_low is not None or min_drawdown is not None:
            new_close = row["close"] * factor
            if max_above_low is not None:
                pct_above_low = pct_above_low_for_simulated_close(
                    panel, idx, new_close, lookback
                )
            if min_drawdown is not None:
                pct_below_high = pct_below_high_for_simulated_close(
                    panel, idx, new_close, high_lookback
                )
        return is_buy_signal(
            spread,
            spread_pct,
            pe_pct,
            index_code,
            pct_above_low=pct_above_low,
            pct_below_high=pct_below_high,
        )

    return _estimate_buy_trigger(check_factor)


def cn_broad_drop_to_buy(snapshot):
    from cn_broad_signal import evaluate_cn_broad_buy

    panel = snapshot.get("panel")
    if panel is None or panel.empty:
        return None

    index_code = snapshot.get("code")
    cfg = get_cn_broad_signal_config(index_code)
    window = cfg["percentile_window"]
    min_days = cfg["percentile_min_days"]
    lookback_days = cfg["buy_low_lookback_days"]
    high_lookback_days = cfg.get("buy_high_lookback_days", 252)
    min_drawdown = cfg.get("buy_min_drawdown_from_high_pct")

    idx = len(panel) - 1
    row = panel.iloc[-1]
    if row["pe"] is None or row["dividend_yield"] is None or row["bond_yield"] is None:
        return None

    def check_factor(factor):
        if factor <= 0:
            return False
        pe = row["pe"] * factor
        div = row["dividend_yield"] / factor
        spread = div - row["bond_yield"]
        pe_pct = rolling_percentile(panel["pe"], idx, pe, window, min_days)
        div_pct = rolling_percentile(
            panel["dividend_yield"], idx, div, window, min_days
        )
        spread_pct = rolling_percentile(
            panel["spread"], idx, spread, window, min_days
        )
        new_close = row["close"] * factor
        pct_above_low = pct_above_low_for_simulated_close(
            panel, idx, new_close, lookback_days
        )
        pct_below_high = (
            pct_below_high_for_simulated_close(
                panel, idx, new_close, high_lookback_days
            )
            if min_drawdown is not None
            else None
        )
        year_range = range_position_for_simulated_close(
            panel, idx, new_close, BUY_RANGE_LOOKBACK_DAYS
        )
        ma_slope = ma_slope_for_simulated_close(
            panel,
            idx,
            new_close,
            cfg.get("buy_trend_ma_days"),
            cfg.get("buy_trend_slope_lookback_days"),
        )
        return evaluate_cn_broad_buy(
            {
                "code": index_code,
                "pe_percentile": pe_pct,
                "pb_percentile": snapshot.get("pb_percentile"),
                "dividend_percentile": div_pct,
                "spread_percentile": spread_pct,
                "pct_above_low": pct_above_low,
                "pct_below_high": pct_below_high,
                "year_range_position": year_range,
                "ma_slope_pct": ma_slope,
            }
        )["is_buy"]

    return _estimate_buy_trigger(check_factor)


def us_index_drop_to_buy(key, snapshot):
    from us_index_signal import evaluate_signal

    panel = snapshot.get("panel")
    if panel is None or panel.empty:
        return None

    idx = len(panel) - 1
    row = panel.iloc[-1]
    if row.get("forward_pe") is None:
        return None

    base = {
        "trailing_pe": row.get("trailing_pe"),
        "forward_pe": row.get("forward_pe"),
        "trailing_pe_percentile": row.get("trailing_pe_percentile"),
        "forward_pe_percentile": row.get("forward_pe_percentile"),
        "us10y_percentile": row.get("us10y_percentile"),
        "implied_growth": row.get("implied_growth"),
        "historical_growth": snapshot.get("historical_growth"),
    }

    daily_window = _cfg_us(key, "DAILY_PERCENTILE_WINDOW")
    daily_min_days = _cfg_us(key, "DAILY_PERCENTILE_MIN_DAYS")
    low_lookback = _cfg_us(key, "BUY_LOW_LOOKBACK_DAYS")
    high_lookback = _cfg_us(key, "BUY_HIGH_LOOKBACK_DAYS")
    trend_ma_days = _cfg_us(key, "BUY_TREND_MA_DAYS")
    trend_slope_days = _cfg_us(key, "BUY_TREND_SLOPE_LOOKBACK_DAYS")

    def check_factor(factor):
        if factor <= 0:
            return False
        forward_pe = row["forward_pe"] * factor
        trailing_pe = (
            row["trailing_pe"] * factor if row.get("trailing_pe") is not None else None
        )
        forward_pct = rolling_percentile(
            panel["forward_pe"],
            idx,
            forward_pe,
            daily_window,
            daily_min_days,
        )
        trailing_pct = None
        if trailing_pe is not None and "trailing_pe" in panel.columns:
            trailing_pct = rolling_percentile(
                panel["trailing_pe"],
                idx,
                trailing_pe,
                daily_window,
                daily_min_days,
            )
        implied = base["implied_growth"]
        if (
            trailing_pe is not None
            and forward_pe is not None
            and forward_pe > 0
            and trailing_pe > 0
        ):
            implied = (trailing_pe / forward_pe) - 1.0

        snap = {
            **base,
            "forward_pe": forward_pe,
            "trailing_pe": trailing_pe,
            "forward_pe_percentile": forward_pct,
            "trailing_pe_percentile": trailing_pct,
            "implied_growth": implied,
        }
        if "close" in panel.columns:
            new_close = row["close"] * factor
            snap["pct_above_low"] = pct_above_low_for_simulated_close(
                panel, idx, new_close, low_lookback
            )
            snap["pct_below_high"] = pct_below_high_for_simulated_close(
                panel, idx, new_close, high_lookback
            )
            snap["year_range_position"] = range_position_for_simulated_close(
                panel, idx, new_close, BUY_RANGE_LOOKBACK_DAYS
            )
            snap["ma_slope_pct"] = ma_slope_for_simulated_close(
                panel, idx, new_close, trend_ma_days, trend_slope_days
            )
        return evaluate_signal(key, snap)["is_buy"]

    return _estimate_buy_trigger(check_factor)


def _cfg_us(key, suffix):
    import config

    return getattr(config, f"{key.upper()}_{suffix}")


def cyb_drop_to_buy(snapshot):
    from cyb_signal import evaluate_cyb_signal

    panel = snapshot.get("panel")
    if panel is None or panel.empty:
        return None

    idx = len(panel) - 1
    row = panel.iloc[-1]
    if row["pe"] is None or row["pb"] is None:
        return None

    def check_factor(factor):
        if factor <= 0:
            return False
        pe = row["pe"] * factor
        pb = row["pb"] * factor
        pe_pct = rolling_percentile(
            panel["pe"], idx, pe, CYB_PERCENTILE_WINDOW, CYB_PERCENTILE_MIN_DAYS
        )
        pb_pct = rolling_percentile(
            panel["pb"], idx, pb, CYB_PERCENTILE_WINDOW, CYB_PERCENTILE_MIN_DAYS
        )
        new_close = row["close"] * factor
        pct_above_low = pct_above_low_for_simulated_close(
            panel, idx, new_close, CYB_BUY_LOW_LOOKBACK_DAYS
        )
        pct_below_high = pct_below_high_for_simulated_close(
            panel, idx, new_close, CYB_BUY_HIGH_LOOKBACK_DAYS
        )
        year_range = range_position_for_simulated_close(
            panel, idx, new_close, BUY_RANGE_LOOKBACK_DAYS
        )
        ma_slope = ma_slope_for_simulated_close(
            panel, idx, new_close, CYB_BUY_TREND_MA_DAYS, CYB_BUY_TREND_SLOPE_LOOKBACK_DAYS
        )
        return evaluate_cyb_signal(
            {
                "pe": pe,
                "pb": pb,
                "pe_percentile": pe_pct,
                "pb_percentile": pb_pct,
                "pct_above_low": pct_above_low,
                "pct_below_high": pct_below_high,
                "year_range_position": year_range,
                "ma_slope_pct": ma_slope,
            }
        )["is_buy"]

    return _estimate_buy_trigger(check_factor)


def _estimate_sell_trigger(still_sell_at_factor, max_move=0.35, precision=0.002):
    """已满足卖出时，估算跌幅使卖出不再成立。"""
    return find_min_drop_breaks_condition(
        lambda d: still_sell_at_factor(1.0 - d),
        max_drop=max_move,
        precision=precision,
    )


def cn_broad_sell_trigger(snapshot):
    from cn_broad_signal import evaluate_cn_broad_sell

    panel = snapshot.get("panel")
    if panel is None or panel.empty:
        return None

    index_code = snapshot.get("code")
    cfg = get_cn_broad_signal_config(index_code)
    window = cfg["percentile_window"]
    min_days = cfg["percentile_min_days"]
    lookback_days = cfg["buy_low_lookback_days"]

    idx = len(panel) - 1
    row = panel.iloc[-1]
    if row["pe"] is None:
        return None

    def check_factor(factor):
        if factor <= 0:
            return False
        pe = row["pe"] * factor
        pe_pct = rolling_percentile(panel["pe"], idx, pe, window, min_days)
        new_close = row["close"] * factor
        pct_above_low = pct_above_low_for_simulated_close(
            panel, idx, new_close, lookback_days
        )
        return evaluate_cn_broad_sell(
            {
                "code": index_code,
                "pe_percentile": pe_pct,
                "pb_percentile": snapshot.get("pb_percentile"),
                "spread_percentile": snapshot.get("spread_percentile"),
                "pct_above_low": pct_above_low,
                "close": new_close,
                "lookback_low_price": snapshot.get("lookback_low_price"),
            }
        )["is_sell"]

    return _estimate_sell_trigger(check_factor)


def cyb_sell_trigger(snapshot):
    from cyb_signal import evaluate_cyb_signal

    panel = snapshot.get("panel")
    if panel is None or panel.empty:
        return None

    idx = len(panel) - 1
    row = panel.iloc[-1]
    if row["pe"] is None or row["pb"] is None:
        return None

    def check_factor(factor):
        if factor <= 0:
            return False
        pe = row["pe"] * factor
        pb = row["pb"] * factor
        pe_pct = rolling_percentile(
            panel["pe"], idx, pe, CYB_PERCENTILE_WINDOW, CYB_PERCENTILE_MIN_DAYS
        )
        pb_pct = rolling_percentile(
            panel["pb"], idx, pb, CYB_PERCENTILE_WINDOW, CYB_PERCENTILE_MIN_DAYS
        )
        return evaluate_cyb_signal(
            {
                "pe": pe,
                "pb": pb,
                "pe_percentile": pe_pct,
                "pb_percentile": pb_pct,
            }
        )["is_sell"]

    return _estimate_sell_trigger(check_factor)
