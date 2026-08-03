"""持仓成本追踪的移动止盈卖点（回测与实盘信号共用）。"""

import pandas as pd

from price_position import price_position_sell_hit

BUY_SIGNAL_COL = "_is_buy_signal"


def row_field(row, name, default=None):
    """兼容 DataFrame.iterrows 的 Series 与 itertuples 的 namedtuple。"""
    getter = getattr(row, "get", None)
    if callable(getter):
        return getter(name, default)
    return getattr(row, name, default)


def attach_buy_signal_column(panel, buy_eval_fn, row_snapshot_fn, col=BUY_SIGNAL_COL):
    """一次性计算买入信号列，供后续峰值/成本统计复用。"""
    if panel is None or panel.empty:
        return panel
    out = panel.copy()
    # itertuples 比 iterrows 快数倍；row_snapshot_fn 需用 row_field 取值
    flags = []
    for row in out.itertuples(index=False):
        snap = row_snapshot_fn(row)
        flags.append(bool(buy_eval_fn(snap).get("is_buy")))
    out[col] = flags
    return out


def compute_recent_signal_buy_avg_from_column(
    panel,
    col=BUY_SIGNAL_COL,
    lookback_days=252,
    close_col="close",
):
    """近 N 个交易日内，策略买入信号日的收盘价算术平均（隐含持仓成本）。"""
    if panel is None or panel.empty or close_col not in panel.columns or col not in panel.columns:
        return None
    work = panel.tail(lookback_days)
    prices = work.loc[work[col], close_col].dropna()
    if prices.empty:
        return None
    return float(prices.astype(float).mean())


def compute_peak_since_last_buy_from_column(
    panel,
    col=BUY_SIGNAL_COL,
    close_col="close",
):
    """自最近一次买入信号日以来，收盘价最高点。"""
    if panel is None or panel.empty or close_col not in panel.columns or col not in panel.columns:
        return None
    buy_rows = panel.loc[panel[col]]
    if buy_rows.empty:
        return None
    closes = panel.loc[buy_rows.index[-1] :, close_col].dropna()
    if closes.empty:
        return None
    return float(closes.astype(float).max())


def last_buy_date_from_column(panel, col=BUY_SIGNAL_COL, date_col="date"):
    """最近一次买入信号日。"""
    if panel is None or panel.empty or col not in panel.columns or date_col not in panel.columns:
        return None
    buy_rows = panel.loc[panel[col]]
    if buy_rows.empty:
        return None
    return buy_rows.iloc[-1][date_col]


def compute_recent_signal_buy_avg(
    panel,
    buy_eval_fn,
    row_snapshot_fn,
    lookback_days=252,
    date_col="date",
    close_col="close",
):
    """近 N 个交易日内，策略买入信号日的收盘价算术平均（隐含持仓成本）。"""
    if panel is None or panel.empty or close_col not in panel.columns:
        return None
    work = panel.tail(lookback_days)
    prices = []
    for _, row in work.iterrows():
        snap = row_snapshot_fn(row)
        if buy_eval_fn(snap).get("is_buy"):
            close = row.get(close_col)
            if close is not None and not pd.isna(close):
                prices.append(float(close))
    if not prices:
        return None
    return sum(prices) / len(prices)


def compute_peak_since_last_buy(
    panel,
    buy_eval_fn,
    row_snapshot_fn,
    date_col="date",
    close_col="close",
):
    """自最近一次买入信号日以来，收盘价最高点。"""
    if panel is None or panel.empty or close_col not in panel.columns:
        return None
    last_buy_idx = None
    for i, row in panel.iterrows():
        snap = row_snapshot_fn(row)
        if buy_eval_fn(snap).get("is_buy"):
            last_buy_idx = i
    if last_buy_idx is None:
        return None
    segment = panel.loc[last_buy_idx:]
    closes = segment[close_col].dropna()
    if closes.empty:
        return None
    return float(closes.max())


def trailing_sell_hit(
    *,
    close,
    cost_basis,
    peak_price,
    min_unrealized_gain_pct,
    trailing_drawdown_pct,
    min_hold_days=None,
    days_since_buy=None,
):
    """浮盈达标后，自持仓期峰值回撤超过阈值则触发移动止盈。"""
    if (
        close is None
        or cost_basis is None
        or cost_basis <= 0
        or peak_price is None
        or peak_price <= 0
        or trailing_drawdown_pct is None
        or min_unrealized_gain_pct is None
    ):
        return False
    gain = (close - cost_basis) / cost_basis
    if gain < min_unrealized_gain_pct:
        return False
    if min_hold_days and days_since_buy is not None and days_since_buy < min_hold_days:
        return False
    drawdown = (peak_price - close) / peak_price
    return drawdown >= trailing_drawdown_pct


def valuation_sell_hit_cn_broad(snapshot, cfg):
    """宽基估值卖点：PE 偏高 + 利差收敛/短期涨幅过大；或近1年区间高位 + PE 不低。"""
    pe_pct = snapshot.get("pe_percentile")
    pb_pct = snapshot.get("pb_percentile")
    spread_pct = snapshot.get("spread_percentile")
    pct_above_low = snapshot.get("pct_above_low")
    year_range = snapshot.get("year_range_position")

    spread_hit = (
        spread_pct is not None
        and spread_pct <= cfg["sell_spread_percentile_max"]
    )
    pe_hit = pe_pct is not None and pe_pct >= cfg["sell_pe_percentile_min"]
    pb_hit = (
        pb_pct is not None and pb_pct >= cfg.get("sell_pb_percentile_min", 99)
    )
    price_hit = price_position_sell_hit(
        pct_above_low, cfg["sell_max_above_low_pct"]
    )
    classic = pe_hit and (spread_hit or price_hit or pb_hit)

    min_range = cfg.get("sell_min_year_range_pct")
    combo_pe_min = cfg.get("sell_pe_combo_min")
    range_hit = (
        min_range is not None
        and year_range is not None
        and year_range >= min_range
    )
    combo_pe_hit = (
        combo_pe_min is not None
        and pe_pct is not None
        and pe_pct >= combo_pe_min
    )
    combo = range_hit and combo_pe_hit and (price_hit or spread_hit or pb_hit)

    return classic or combo


def simulate_trades_trailing(
    panel,
    start_date,
    end_date=None,
    amount=100.0,
    date_col="date",
    buy_fn=None,
    valuation_sell_fn=None,
    trailing_cfg=None,
    valuation_price_col=None,
):
    """按日模拟买入；卖出支持移动止盈（优先）+ 可选估值卖点。"""
    from backtest_trade_signals import _filter_panel, _resolve_buy_amount

    val_col = valuation_price_col or "close"
    sample = _filter_panel(panel, start_date, end_date, date_col=date_col)
    if sample.empty:
        return None
    if val_col not in sample.columns:
        val_col = "close"

    latest = sample.iloc[-1]
    latest_price = float(latest[val_col])
    latest_date = latest["_dt"]

    units = 0.0
    buy_only_units = 0.0
    total_bought = 0.0
    total_sold = 0.0
    cost_basis = 0.0
    peak_since_buy = 0.0
    last_buy_dt = None
    buy_count = 0
    sell_count = 0
    buy_dates = []
    sell_dates = []

    trail = trailing_cfg or {}
    use_trailing = trail.get("trailing_drawdown_pct") is not None

    cols = sample.columns.tolist()
    for tup in sample.itertuples(index=False, name=None):
        row = dict(zip(cols, tup))
        price = float(row[val_col])
        dt = row["_dt"]
        day = dt.strftime("%Y-%m-%d")
        is_buy = buy_fn(row) if buy_fn else False

        if units > 0:
            peak_since_buy = max(peak_since_buy, price)

        is_sell = False
        if units > 0:
            days_since = (dt - last_buy_dt).days if last_buy_dt is not None else None
            avg_cost = cost_basis / units if units > 0 else None
            if use_trailing and avg_cost is not None:
                is_sell = trailing_sell_hit(
                    close=price,
                    cost_basis=avg_cost,
                    peak_price=peak_since_buy,
                    min_unrealized_gain_pct=trail.get("min_unrealized_gain_pct"),
                    trailing_drawdown_pct=trail.get("trailing_drawdown_pct"),
                    min_hold_days=trail.get("min_hold_days"),
                    days_since_buy=days_since,
                )
            if not is_sell and valuation_sell_fn:
                is_sell = valuation_sell_fn(row)

        buy_amount = _resolve_buy_amount(amount, row) if is_buy else 0.0
        if is_buy and buy_amount > 0:
            units += buy_amount / price
            buy_only_units += buy_amount / price
            total_bought += buy_amount
            cost_basis += buy_amount
            buy_count += 1
            buy_dates.append(day)
            last_buy_dt = dt
            peak_since_buy = price
        elif is_sell and units > 0:
            total_sold += units * price
            units = 0.0
            cost_basis = 0.0
            peak_since_buy = 0.0
            sell_count += 1
            sell_dates.append(day)

    final_value = total_sold + units * latest_price
    profit = final_value - total_bought
    return_pct = profit / total_bought * 100 if total_bought > 0 else None

    buy_only_value = buy_only_units * latest_price
    buy_only_profit = buy_only_value - total_bought
    buy_only_return_pct = (
        buy_only_profit / total_bought * 100 if total_bought > 0 else None
    )

    return {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_bought": total_bought,
        "total_sold": total_sold,
        "final_units": units,
        "final_price": latest_price,
        "final_date": latest_date,
        "final_value": final_value,
        "profit": profit,
        "return_pct": return_pct,
        "buy_only_value": buy_only_value,
        "buy_only_profit": buy_only_profit,
        "buy_only_return_pct": buy_only_return_pct,
        "buy_dates": buy_dates,
        "sell_dates": sell_dates,
    }
