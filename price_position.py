"""收盘价相对近期低点的位置（各指数买入过滤共用）。"""

import pandas as pd

from signal_format import make_criterion


def attach_pct_above_low(panel, lookback_days=60, close_col="close"):
    """为面板增加 pct_above_low 列（收盘价 / N 日低点 - 1）。"""
    out = panel.copy()
    min_periods = min(20, max(1, lookback_days // 3))
    low_n = out[close_col].rolling(lookback_days, min_periods=min_periods).min()
    out["pct_above_low"] = out[close_col] / low_n - 1
    return out


def attach_pct_below_high(panel, lookback_days=252, close_col="close"):
    """为面板增加 pct_below_high 列（1 - 收盘价 / N 日高点，即距高点回撤比例）。"""
    out = panel.copy()
    min_periods = min(20, max(1, lookback_days // 3))
    high_n = out[close_col].rolling(lookback_days, min_periods=min_periods).max()
    out["pct_below_high"] = 1 - out[close_col] / high_n
    return out


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


def make_drawdown_from_high_criterion(
    pct_below_high, min_drawdown_pct, lookback_days
):
    """生成报告用距高点回撤判定行；未启用过滤时返回 None。"""
    if min_drawdown_pct is None:
        return None
    dd_ok = drawdown_from_high_ok(pct_below_high, min_drawdown_pct)
    return make_criterion(
        f"距{lookback_days}日高点回撤",
        dd_ok,
        (
            f"{pct_below_high * 100:.1f}%（需≥{min_drawdown_pct * 100:.0f}%）"
            if pct_below_high is not None and not pd.isna(pct_below_high)
            else "—"
        ),
        "指数距近期高点回撤不足",
        applicable=pct_below_high is not None and not pd.isna(pct_below_high),
    )


def make_price_position_criterion(pct_above_low, max_above_low_pct, lookback_days):
    """生成报告用判定行；未启用过滤时返回 None。"""
    if max_above_low_pct is None:
        return None
    price_ok = price_position_ok(pct_above_low, max_above_low_pct)
    return make_criterion(
        f"距{lookback_days}日低点涨幅",
        price_ok,
        (
            f"{pct_above_low * 100:.1f}%（需≤{max_above_low_pct * 100:.0f}%）"
            if pct_above_low is not None and not pd.isna(pct_above_low)
            else "—"
        ),
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


def make_sell_price_position_criterion(pct_above_low, min_above_low_pct, lookback_days):
    """生成卖出侧价格位置判定行。"""
    if min_above_low_pct is None:
        return None
    sell_hit = price_position_sell_hit(pct_above_low, min_above_low_pct)
    return make_criterion(
        f"距{lookback_days}日低点涨幅",
        sell_hit,
        (
            f"{pct_above_low * 100:.1f}%（需≥{min_above_low_pct * 100:.0f}%）"
            if pct_above_low is not None and not pd.isna(pct_above_low)
            else "—"
        ),
        "指数尚未从近期低点反弹足够",
        applicable=pct_above_low is not None and not pd.isna(pct_above_low),
    )
