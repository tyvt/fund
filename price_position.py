"""收盘价相对近期低点的位置（各指数买入过滤共用）。"""

import pandas as pd

from signal_format import make_criterion


def _format_pct_threshold(value):
    """百分比阈值展示（保留 1 位小数，避免 1.5% 显示成 1%）。"""
    pct = value * 100
    if abs(pct - round(pct)) < 0.05:
        return f"{pct:.0f}%"
    return f"{pct:.1f}%"


def format_index_price(value):
    """报告用指数点位展示。"""
    if value is None or pd.isna(value):
        return "—"
    v = float(value)
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 100:
        return f"{v:.2f}"
    return f"{v:.4f}"


def row_price_position_fields(row, close_col="close"):
    """从面板行提取收盘价与滚动高低点（供 snapshot 合并）。"""

    def _pick(key):
        if row is None:
            return None
        val = row.get(key)
        if val is None or pd.isna(val):
            return None
        return float(val)

    close = _pick(close_col)
    if close is None:
        close = _pick("close")
    return {
        "close": close,
        "lookback_low_price": _pick("lookback_low_price"),
        "lookback_high_price": _pick("lookback_high_price"),
        "range_low_price": _pick("range_low_price"),
        "range_high_price": _pick("range_high_price"),
    }


def _max_close_above_low(low_price, max_above_low_pct):
    if low_price is None or max_above_low_pct is None:
        return None
    return low_price * (1 + max_above_low_pct)


def _min_close_for_drawdown(high_price, min_drawdown_pct):
    if high_price is None or min_drawdown_pct is None:
        return None
    return high_price * (1 - min_drawdown_pct)


def _max_close_in_year_range(range_low, range_high, max_year_range_pct):
    if (
        range_low is None
        or range_high is None
        or max_year_range_pct is None
        or range_high <= range_low
    ):
        return None
    return range_low + max_year_range_pct * (range_high - range_low)


def _min_close_sell_above_low(low_price, min_above_low_pct):
    if low_price is None or min_above_low_pct is None:
        return None
    return low_price * (1 + min_above_low_pct)


def attach_pct_above_low(panel, lookback_days=60, close_col="close"):
    """为面板增加 pct_above_low 列（收盘价 / N 日低点 - 1）及 lookback_low_price。"""
    out = panel.copy()
    min_periods = min(20, max(1, lookback_days // 3))
    low_n = out[close_col].rolling(lookback_days, min_periods=min_periods).min()
    out["lookback_low_price"] = low_n
    out["pct_above_low"] = out[close_col] / low_n - 1
    return out


def attach_pct_below_high(panel, lookback_days=252, close_col="close"):
    """为面板增加 pct_below_high 列及 lookback_high_price。"""
    out = panel.copy()
    min_periods = min(20, max(1, lookback_days // 3))
    high_n = out[close_col].rolling(lookback_days, min_periods=min_periods).max()
    out["lookback_high_price"] = high_n
    out["pct_below_high"] = 1 - out[close_col] / high_n
    return out


def attach_year_range_position(
    panel, lookback_days=252, close_col="close", date_col="date"
):
    """滚动 N 日高低点，计算区间位置（0=窗口内最低，1=窗口内最高）。

    date_col 保留以兼容旧调用，计算本身不依赖日历切片。
    """
    del date_col  # 兼容旧签名，区间位置按滚动窗口而非自然年
    out = panel.copy()
    min_periods = min(20, max(1, lookback_days // 3))
    low_n = out[close_col].rolling(lookback_days, min_periods=min_periods).min()
    high_n = out[close_col].rolling(lookback_days, min_periods=min_periods).max()
    out["range_low_price"] = low_n
    out["range_high_price"] = high_n
    span = (high_n - low_n).replace(0, pd.NA)
    out["year_range_position"] = pd.to_numeric(
        (out[close_col].astype(float) - low_n) / span, errors="coerce"
    )
    return out


def attach_ma_trend(panel, ma_days=200, slope_lookback=60, close_col="close"):
    """为面板增加均线趋势列：ma_slope_pct（N日MA变化率）、below_ma。"""
    out = panel.copy()
    min_periods = min(ma_days // 2, max(20, ma_days // 3))
    ma = out[close_col].rolling(ma_days, min_periods=min_periods).mean()
    ma_lag = ma.shift(slope_lookback)
    out["ma_trend"] = ma
    out["ma_slope_pct"] = pd.to_numeric(
        (ma - ma_lag) / ma_lag.replace(0, pd.NA), errors="coerce"
    )
    out["below_ma"] = out[close_col].astype(float) < ma
    return out


def trend_filter_ok(
    ma_slope_pct,
    year_range_position,
    min_ma_slope_pct=-0.025,
    downtrend_max_range_pct=0.12,
):
    """MA 企稳/向上可买；深度下行中仅允许近1年极低位买入。"""
    if ma_slope_pct is None or pd.isna(ma_slope_pct):
        return True
    if ma_slope_pct >= min_ma_slope_pct:
        return True
    if year_range_position is not None and not pd.isna(year_range_position):
        return year_range_position <= downtrend_max_range_pct
    return False


def make_trend_criterion(
    ma_slope_pct,
    year_range_position,
    min_ma_slope_pct,
    downtrend_max_range_pct,
    ma_days=200,
    slope_lookback=60,
):
    """生成报告用均线趋势判定行。"""
    ok = trend_filter_ok(
        ma_slope_pct, year_range_position, min_ma_slope_pct, downtrend_max_range_pct
    )
    slope_text = (
        f"{ma_slope_pct * 100:.1f}%"
        if ma_slope_pct is not None and not pd.isna(ma_slope_pct)
        else "—"
    )
    return make_criterion(
        f"MA{ma_days}趋势",
        ok,
        (
            f"斜率{slope_text}（需≥{min_ma_slope_pct * 100:.1f}%，"
            f"否则区间需≤{downtrend_max_range_pct * 100:.0f}%）"
        ),
        "中长期均线仍下行，非极低位不买入",
        applicable=ma_slope_pct is not None and not pd.isna(ma_slope_pct),
    )


def ma_slope_for_simulated_close(
    panel, idx, new_close, ma_days, slope_lookback, close_col="close"
):
    """推演价格变动后的 MA 斜率。"""
    need = ma_days + slope_lookback
    start = max(0, idx - need + 1)
    window = panel[close_col].iloc[start : idx + 1].astype(float).tolist()
    if len(window) < ma_days:
        return None
    window[-1] = new_close

    def _ma(closes, end_idx):
        seg = closes[max(0, end_idx - ma_days) : end_idx]
        if len(seg) < ma_days:
            return None
        return sum(seg) / len(seg)

    ma_now = _ma(window, len(window))
    lag_end = len(window) - slope_lookback
    ma_lag = _ma(window, lag_end)
    if ma_now is None or ma_lag is None or ma_lag <= 0:
        return None
    return (ma_now - ma_lag) / ma_lag


def compute_pct_above_low_from_prices(prices, lookback_days=60, close_col="close"):
    """从价格序列计算最新一日的距低点涨幅。"""
    if prices is None or prices.empty or close_col not in prices.columns:
        return None
    work = prices.copy()
    if "date" in work.columns:
        work = work.sort_values("date")
    work = work.reset_index(drop=True)
    min_periods = min(20, max(1, lookback_days // 3))
    low_n = work[close_col].rolling(lookback_days, min_periods=min_periods).min()
    latest_low = low_n.iloc[-1]
    latest_close = work[close_col].iloc[-1]
    if pd.isna(latest_low) or pd.isna(latest_close) or latest_low <= 0:
        return None
    return float(latest_close) / float(latest_low) - 1


def price_position_ok(pct_above_low, max_above_low_pct):
    """距低点涨幅是否在允许范围内。"""
    if max_above_low_pct is None:
        return True
    if pct_above_low is None or pd.isna(pct_above_low):
        return False
    return pct_above_low <= max_above_low_pct


def drawdown_from_high_ok(pct_below_high, min_drawdown_pct):
    """距高点回撤是否达到买入门槛。"""
    if min_drawdown_pct is None:
        return True
    if pct_below_high is None or pd.isna(pct_below_high):
        return False
    return pct_below_high >= min_drawdown_pct


def year_range_ok(year_range_position, max_year_range_pct):
    """滚动区间位置是否在允许上限内（越低越接近窗口内低点）。"""
    if max_year_range_pct is None:
        return True
    if year_range_position is None or pd.isna(year_range_position):
        return False
    return year_range_position <= max_year_range_pct


def is_near_year_low(year_range_position, near_threshold=0.15):
    """是否处于滚动区间下沿（用于低位放宽估值/回撤门槛）。"""
    if year_range_position is None or pd.isna(year_range_position):
        return False
    return year_range_position <= near_threshold


def effective_drawdown_threshold(
    min_drawdown_pct, year_range_position, near_threshold=0.15, waive_near_low=True
):
    """接近滚动区间低点时可豁免距高点回撤要求。"""
    if (
        waive_near_low
        and min_drawdown_pct is not None
        and is_near_year_low(year_range_position, near_threshold)
    ):
        return None
    return min_drawdown_pct


def effective_max_above_low_pct(
    max_above_low_pct,
    year_range_position,
    near_threshold=0.15,
    near_low_relax=0.0,
    mid_range_threshold=0.35,
    mid_range_cap=0.02,
):
    """近1年低位放宽距低点涨幅；中高位收紧，避免反弹途中追高。"""
    if max_above_low_pct is None:
        return None
    if is_near_year_low(year_range_position, near_threshold):
        return max_above_low_pct + near_low_relax
    if (
        year_range_position is not None
        and not pd.isna(year_range_position)
        and year_range_position > mid_range_threshold
    ):
        return min(max_above_low_pct, mid_range_cap)
    return max_above_low_pct


def range_position_for_simulated_close(
    panel, idx, new_close, lookback_days, close_col="close"
):
    """推演价格变动后的近 N 日区间位置。"""
    start = max(0, idx - lookback_days + 1)
    window = panel[close_col].iloc[start : idx + 1].astype(float).tolist()
    window[-1] = new_close
    low_n = min(window)
    high_n = max(window)
    if high_n <= low_n:
        return 0.0
    return (new_close - low_n) / (high_n - low_n)


def format_range_position_text(
    year_range_position, lookback_days=252, close=None, range_low=None, range_high=None
):
    """报告用区间位置文案（优先展示价格区间）。"""
    label = "近1年" if lookback_days == 252 else f"近{lookback_days}日"
    if range_low is not None and range_high is not None:
        return f"{label} {format_index_price(range_low)}–{format_index_price(range_high)}"
    if year_range_position is None or pd.isna(year_range_position):
        return "—"
    return f"{label}区间 {year_range_position * 100:.0f}%"


def format_price_position_line(snapshot, lookback_days=252):
    """报告用价格位置摘要行（展示点位，非涨跌幅）。"""
    close = snapshot.get("close")
    low = snapshot.get("lookback_low_price")
    high = snapshot.get("lookback_high_price")
    range_low = snapshot.get("range_low_price")
    range_high = snapshot.get("range_high_price")
    parts = []
    if close is not None and not pd.isna(close):
        price_label = "现价" if snapshot.get("live_price") else "收盘"
        parts.append(f"{price_label} {format_index_price(close)}")
    parts.append(
        format_range_position_text(
            snapshot.get("year_range_position"),
            lookback_days,
            close=close,
            range_low=range_low,
            range_high=range_high,
        )
    )
    if low is not None and not pd.isna(low):
        parts.append(f"近{lookback_days}日低 {format_index_price(low)}")
    high_days = snapshot.get("high_lookback_days", lookback_days)
    if high is not None and not pd.isna(high):
        parts.append(f"近{high_days}日高 {format_index_price(high)}")
    slope = snapshot.get("ma_slope_pct")
    if slope is not None and not pd.isna(slope):
        parts.append(f"MA200斜率 {slope * 100:.1f}%")
    return " | ".join(parts)


def pct_above_low_for_simulated_close(
    panel, idx, new_close, lookback_days, close_col="close"
):
    """推演价格下跌后的距低点涨幅。"""
    start = max(0, idx - lookback_days + 1)
    window = panel[close_col].iloc[start : idx + 1].astype(float).tolist()
    window[-1] = new_close
    low_n = min(window)
    if low_n <= 0:
        return None
    return new_close / low_n - 1


def pct_below_high_for_simulated_close(
    panel, idx, new_close, lookback_days, close_col="close"
):
    """推演价格下跌后的距高点回撤。"""
    start = max(0, idx - lookback_days + 1)
    window = panel[close_col].iloc[start : idx + 1].astype(float).tolist()
    window[-1] = new_close
    high_n = max(window)
    if high_n <= 0:
        return None
    return 1 - new_close / high_n


def make_year_range_criterion(
    year_range_position,
    max_year_range_pct,
    lookback_days=252,
    close=None,
    range_low=None,
    range_high=None,
):
    """生成报告用滚动区间位置判定行。"""
    if max_year_range_pct is None:
        return None
    ok = year_range_ok(year_range_position, max_year_range_pct)
    label = "近1年价格区间" if lookback_days == 252 else f"近{lookback_days}日价格区间"
    max_close = _max_close_in_year_range(range_low, range_high, max_year_range_pct)
    if close is not None and range_low is not None and range_high is not None:
        detail = (
            f"收盘 {format_index_price(close)}（区间 "
            f"{format_index_price(range_low)}–{format_index_price(range_high)}，"
            f"买入需≤{format_index_price(max_close)}）"
        )
    elif year_range_position is not None and not pd.isna(year_range_position):
        detail = (
            f"{year_range_position * 100:.0f}%（需≤{max_year_range_pct * 100:.0f}%）"
        )
    else:
        detail = "—"
    return make_criterion(
        label,
        ok,
        detail,
        "指数处于近1年偏高位置" if lookback_days == 252 else "指数处于近期偏高位置",
        applicable=year_range_position is not None and not pd.isna(year_range_position),
    )


def make_drawdown_from_high_criterion(
    pct_below_high,
    min_drawdown_pct,
    lookback_days,
    close=None,
    lookback_high=None,
):
    """生成报告用距高点回撤判定行；未启用过滤时返回 None。"""
    if min_drawdown_pct is None:
        return None
    dd_ok = drawdown_from_high_ok(pct_below_high, min_drawdown_pct)
    min_close = _min_close_for_drawdown(lookback_high, min_drawdown_pct)
    if close is not None and lookback_high is not None:
        detail = (
            f"收盘 {format_index_price(close)}（近{lookback_days}日高 "
            f"{format_index_price(lookback_high)}，买入需≤{format_index_price(min_close)}）"
        )
    elif pct_below_high is not None and not pd.isna(pct_below_high):
        detail = (
            f"{pct_below_high * 100:.1f}%（需≥{min_drawdown_pct * 100:.0f}%）"
        )
    else:
        detail = "—"
    return make_criterion(
        f"近{lookback_days}日高点",
        dd_ok,
        detail,
        "指数距近期高点回撤不足",
        applicable=pct_below_high is not None and not pd.isna(pct_below_high),
    )


def make_price_position_criterion(
    pct_above_low,
    max_above_low_pct,
    lookback_days,
    close=None,
    lookback_low=None,
):
    """生成报告用判定行；未启用过滤时返回 None。"""
    if max_above_low_pct is None:
        return None
    price_ok = price_position_ok(pct_above_low, max_above_low_pct)
    max_close = _max_close_above_low(lookback_low, max_above_low_pct)
    if close is not None and lookback_low is not None:
        detail = (
            f"收盘 {format_index_price(close)}（近{lookback_days}日低 "
            f"{format_index_price(lookback_low)}，买入需≤{format_index_price(max_close)}）"
        )
    elif pct_above_low is not None and not pd.isna(pct_above_low):
        detail = (
            f"{pct_above_low * 100:.1f}%（需≤{_format_pct_threshold(max_above_low_pct)}）"
        )
    else:
        detail = "—"
    return make_criterion(
        f"近{lookback_days}日低点",
        price_ok,
        detail,
        "指数已从近期低点反弹较多",
        applicable=pct_above_low is not None and not pd.isna(pct_above_low),
    )


def price_position_sell_hit(pct_above_low, min_above_low_pct):
    """距低点涨幅是否达到卖出门槛。"""
    if min_above_low_pct is None:
        return False
    if pct_above_low is None or pd.isna(pct_above_low):
        return False
    return pct_above_low >= min_above_low_pct


def build_buy_price_ceilings(
    snapshot,
    max_above_low_pct,
    min_drawdown_pct,
    max_year_range_pct,
    low_lookback_days=90,
    high_lookback_days=252,
    range_lookback_days=252,
):
    """从快照提取各价格类买入条件的上限点位（收盘需≤该值）。"""
    ceilings = []
    low = snapshot.get("lookback_low_price")
    high = snapshot.get("lookback_high_price")
    range_low = snapshot.get("range_low_price")
    range_high = snapshot.get("range_high_price")

    if max_above_low_pct is not None and low is not None and not pd.isna(low):
        ceilings.append(
            (f"近{low_lookback_days}日低", _max_close_above_low(low, max_above_low_pct))
        )
    if min_drawdown_pct is not None and high is not None and not pd.isna(high):
        ceilings.append(
            (
                f"近{high_lookback_days}日高",
                _min_close_for_drawdown(high, min_drawdown_pct),
            )
        )
    if (
        max_year_range_pct is not None
        and range_low is not None
        and range_high is not None
        and not pd.isna(range_low)
        and not pd.isna(range_high)
    ):
        label = "近1年区间" if range_lookback_days == 252 else f"近{range_lookback_days}日区间"
        ceilings.append(
            (
                label,
                _max_close_in_year_range(range_low, range_high, max_year_range_pct),
            )
        )
    return ceilings


def build_sell_price_floors(snapshot, min_above_low_pct, lookback_days=120):
    """从快照提取卖出侧价格下限（收盘需≥该值才维持卖出）。"""
    floors = []
    low = snapshot.get("lookback_low_price")
    if min_above_low_pct is not None and low is not None and not pd.isna(low):
        floors.append(
            (f"近{lookback_days}日低", _min_close_sell_above_low(low, min_above_low_pct))
        )
    return floors


def format_price_bound_summary(bounds, bound_kind="上限"):
    """将 (标签, 点位) 列表格式化为报告用摘要。"""
    valid = [(label, price) for label, price in bounds if price is not None]
    if not valid:
        return None
    tightest = min(valid, key=lambda x: x[1]) if bound_kind == "上限" else max(valid, key=lambda x: x[1])
    label, price = tightest
    text = f"{label} {bound_kind} {format_index_price(price)}"
    extras = []
    for other_label, other_price in valid:
        if other_label == label:
            continue
        extras.append(f"{other_label} {bound_kind} {format_index_price(other_price)}")
    if extras:
        text += "；" + "；".join(extras)
    return text


def make_sell_price_position_criterion(
    pct_above_low,
    min_above_low_pct,
    lookback_days,
    close=None,
    lookback_low=None,
):
    """生成卖出侧价格位置判定行。"""
    if min_above_low_pct is None:
        return None
    sell_hit = price_position_sell_hit(pct_above_low, min_above_low_pct)
    min_close = _min_close_sell_above_low(lookback_low, min_above_low_pct)
    if close is not None and lookback_low is not None:
        detail = (
            f"收盘 {format_index_price(close)}（近{lookback_days}日低 "
            f"{format_index_price(lookback_low)}，卖出需≥{format_index_price(min_close)}）"
        )
    elif pct_above_low is not None and not pd.isna(pct_above_low):
        detail = (
            f"{pct_above_low * 100:.1f}%（需≥{min_above_low_pct * 100:.0f}%）"
        )
    else:
        detail = "—"
    return make_criterion(
        f"近{lookback_days}日低点",
        sell_hit,
        detail,
        "指数尚未从近期低点反弹足够",
        applicable=pct_above_low is not None and not pd.isna(pct_above_low),
    )
