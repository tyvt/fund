"""将实时价叠加到日频 snapshot，并重算价格相关指标。"""

from __future__ import annotations

import pandas as pd

from price_position import (
    ma_slope_for_simulated_close,
    pct_above_low_for_simulated_close,
    pct_below_high_for_simulated_close,
    range_position_for_simulated_close,
    row_price_position_fields,
)
from realtime_quote import LiveQuote


def resolve_live_price_params(index_code: str) -> dict:
    """按指数类型返回价格位置回看参数。"""
    from config import (
        BUY_RANGE_LOOKBACK_DAYS,
        BUY_TREND_MA_DAYS,
        BUY_TREND_SLOPE_LOOKBACK_DAYS,
        CYB_BUY_HIGH_LOOKBACK_DAYS,
        CYB_BUY_LOW_LOOKBACK_DAYS,
        CYB_BUY_TREND_MA_DAYS,
        CYB_BUY_TREND_SLOPE_LOOKBACK_DAYS,
        INDICES,
        get_cn_broad_signal_config,
        get_dividend_signal_config,
    )

    if index_code == "399006":
        return {
            "low_lookback_days": CYB_BUY_LOW_LOOKBACK_DAYS,
            "high_lookback_days": CYB_BUY_HIGH_LOOKBACK_DAYS,
            "range_lookback_days": BUY_RANGE_LOOKBACK_DAYS,
            "ma_days": CYB_BUY_TREND_MA_DAYS,
            "slope_lookback": CYB_BUY_TREND_SLOPE_LOOKBACK_DAYS,
            "close_col": "close",
        }
    if index_code in ("NDX", "SPX"):
        from us_index_data import _cfg

        key = "ndx" if index_code == "NDX" else "spx"
        return {
            "low_lookback_days": _cfg(key, "BUY_LOW_LOOKBACK_DAYS"),
            "high_lookback_days": _cfg(key, "BUY_HIGH_LOOKBACK_DAYS"),
            "range_lookback_days": BUY_RANGE_LOOKBACK_DAYS,
            "ma_days": BUY_TREND_MA_DAYS,
            "slope_lookback": BUY_TREND_SLOPE_LOOKBACK_DAYS,
            "close_col": "close",
        }
    if index_code in {i["code"] for i in INDICES}:
        cfg = get_dividend_signal_config(index_code)
        return {
            "low_lookback_days": cfg["buy_low_lookback_days"],
            "high_lookback_days": cfg["buy_high_lookback_days"],
            "range_lookback_days": BUY_RANGE_LOOKBACK_DAYS,
            "ma_days": None,
            "slope_lookback": None,
            "close_col": "close",
        }

    cfg = get_cn_broad_signal_config(index_code)
    return {
        "low_lookback_days": cfg["buy_low_lookback_days"],
        "high_lookback_days": cfg.get("buy_high_lookback_days", 252),
        "range_lookback_days": BUY_RANGE_LOOKBACK_DAYS,
        "ma_days": cfg.get("buy_trend_ma_days"),
        "slope_lookback": cfg.get("buy_trend_slope_lookback_days"),
        "close_col": "close",
    }


def _window_extrema(panel, idx, new_close, lookback_days, close_col="close"):
    start = max(0, idx - lookback_days + 1)
    window = panel[close_col].iloc[start : idx + 1].astype(float).tolist()
    if not window:
        return None, None
    window[-1] = new_close
    return min(window), max(window)


def _ma_at_close(panel, idx, new_close, ma_days, close_col="close"):
    if not ma_days:
        return None
    start = max(0, idx - ma_days + 1)
    seg = panel[close_col].iloc[start : idx + 1].astype(float).tolist()
    if not seg:
        return None
    seg[-1] = new_close
    return sum(seg) / len(seg)


def apply_live_quote(
    snapshot: dict,
    quote: LiveQuote,
    *,
    params: dict | None = None,
) -> dict:
    """用实时价覆盖 snapshot 收盘价并重算价格位置类字段。"""
    panel = snapshot.get("panel")
    if panel is None or panel.empty:
        return snapshot

    index_code = snapshot.get("code") or quote.index_code
    cfg = params or resolve_live_price_params(index_code)
    close_col = cfg.get("close_col", "close")
    if close_col not in panel.columns:
        return snapshot

    out = dict(snapshot)
    panel_copy = panel.copy()
    idx = len(panel_copy) - 1
    prev_close = float(out.get("close") or panel_copy.iloc[idx][close_col])
    live_close = float(quote.price)

    panel_copy.at[panel_copy.index[idx], close_col] = live_close
    out["panel"] = panel_copy
    out["close_prev"] = (
        float(quote.prev_close)
        if quote.prev_close and quote.prev_close > 0
        else prev_close
    )
    out["close"] = live_close
    out["live_price"] = True
    out["live_quote_time"] = quote.quote_time
    if out["close_prev"] > 0:
        out["live_price_delta_pct"] = live_close / out["close_prev"] - 1
    else:
        out["live_price_delta_pct"] = None

    low_lb = cfg["low_lookback_days"]
    high_lb = cfg["high_lookback_days"]
    range_lb = cfg["range_lookback_days"]

    out["pct_above_low"] = pct_above_low_for_simulated_close(
        panel_copy, idx, live_close, low_lb, close_col=close_col
    )
    out["pct_below_high"] = pct_below_high_for_simulated_close(
        panel_copy, idx, live_close, high_lb, close_col=close_col
    )
    out["year_range_position"] = range_position_for_simulated_close(
        panel_copy, idx, live_close, range_lb, close_col=close_col
    )

    ma_days = cfg.get("ma_days")
    slope_lookback = cfg.get("slope_lookback")
    if ma_days and slope_lookback:
        out["ma_slope_pct"] = ma_slope_for_simulated_close(
            panel_copy,
            idx,
            live_close,
            ma_days,
            slope_lookback,
            close_col=close_col,
        )
        ma_val = _ma_at_close(panel_copy, idx, live_close, ma_days, close_col=close_col)
        out["below_ma"] = (live_close < ma_val) if ma_val is not None else out.get("below_ma")

    low_price, _ = _window_extrema(panel_copy, idx, live_close, low_lb, close_col=close_col)
    _, high_price = _window_extrema(
        panel_copy, idx, live_close, high_lb, close_col=close_col
    )
    range_low, range_high = _window_extrema(
        panel_copy, idx, live_close, range_lb, close_col=close_col
    )
    out["lookback_low_price"] = low_price
    out["lookback_high_price"] = high_price
    out["range_low_price"] = range_low
    out["range_high_price"] = range_high
    out.update(row_price_position_fields(out))
    return out


def maybe_apply_live(
    snapshot: dict,
    quotes: dict[str, LiveQuote] | None = None,
) -> dict:
    """若该指数有实时行情则叠加到 snapshot；无数据则原样返回。"""
    if not quotes:
        return snapshot
    code = snapshot.get("code")
    if not code:
        return snapshot
    quote = quotes.get(code)
    if quote is None:
        return snapshot
    return apply_live_quote(snapshot, quote)


def format_live_meta_extra(
    snapshot: dict, *, quotes_attempted: bool = False
) -> str | None:
    """报告元信息行附加：实时价与时间；已请求但失败时标「实时暂无」。"""
    if not snapshot.get("live_price"):
        return "实时暂无" if quotes_attempted else None
    from price_position import format_index_price

    close = snapshot.get("close")
    delta = snapshot.get("live_price_delta_pct")
    quote_time = snapshot.get("live_quote_time")
    parts = [f"实时 {format_index_price(close)}"]
    if delta is not None and not pd.isna(delta):
        parts.append(f"{delta * 100:+.2f}%")
    if quote_time:
        parts.append(str(quote_time))
    return " ".join(parts)
