"""诊断卖出信号稀疏问题，并回测多种修复方案（2016-2025）。"""

import sys
from dataclasses import dataclass

import pandas as pd

from backtest_buy_signals import BacktestPanels, CN_BROAD_BACKTEST_INDICES, US_INDEX_META, _us_buy_snapshot
from backtest_trade_signals import _cn_broad_signals, _cyb_signals, _filter_panel, _hstech_signals, simulate_trades
from cn_broad_signal import evaluate_cn_broad_buy, evaluate_cn_broad_sell
from config import CYB_INDEX, HSTECH_INDEX, INDICES, resolve_backtest_amounts
from cyb_signal import evaluate_cyb_signal
from dividend_data import is_buy_signal_row
from market_data import configure_stdout_utf8

START, END = "2016-01-01", "2025-12-31"


def _cn_snap(row, code):
    return {
        "code": code,
        "pe_percentile": row.get("pe_percentile"),
        "pb_percentile": row.get("pb_percentile"),
        "dividend_percentile": row.get("dividend_percentile"),
        "spread_percentile": row.get("spread_percentile"),
        "pct_above_low": row.get("pct_above_low"),
        "pct_below_high": row.get("pct_below_high"),
        "year_range_position": row.get("year_range_position"),
        "ma_slope_pct": row.get("ma_slope_pct"),
        "close": row.get("close"),
    }


def diagnose_hs300_2021():
    panels = BacktestPanels()
    panel = panels.cn_broad_panel("000300")
    sample = panel[(pd.to_datetime(panel["date"]) >= "2021-01-01") & (pd.to_datetime(panel["date"]) <= "2021-03-31")]
    print("\n=== 沪深300 2021年1-3月 卖出诊断 ===")
    print(f"{'日期':<12} {'PE分位':>8} {'利差分位':>8} {'距低点':>8} {'卖出?':>6}")
    for _, row in sample.iterrows():
        ev = evaluate_cn_broad_buy(_cn_snap(row, "000300"))
        sell = evaluate_cn_broad_sell(_cn_snap(row, "000300"))
        d = pd.Timestamp(row["date"]).strftime("%Y-%m-%d")
        pe = row.get("pe_percentile")
        sp = row.get("spread_percentile")
        al = row.get("pct_above_low")
        if pe is not None and pe >= 75:
            print(
                f"{d:<12} {pe:>7.1f}% {sp if sp is None else f'{sp:.1f}%':>8} "
                f"{al*100 if al else 0:>7.1f}% {'是' if sell['is_sell'] else '否':>6}"
            )


def diagnose_hstech_2022_oct():
    panels = BacktestPanels()
    panel = panels.hstech_panel()
    sample = panel[(pd.to_datetime(panel["date"]) >= "2022-09-01") & (pd.to_datetime(panel["date"]) <= "2022-11-30")]
    from hstech_signal import evaluate_hstech_signal

    print("\n=== 恒科 2022年9-11月 买入诊断 ===")
    for _, row in sample.iterrows():
        snap = {
            "pe": row["pe"], "pe_percentile": row.get("pe_percentile"),
            "dividend_percentile": row.get("dividend_percentile"),
            "pct_above_low": row.get("pct_above_low"),
            "pct_below_high": row.get("pct_below_high"),
            "year_range_position": row.get("year_range_position"),
            "ma_slope_pct": row.get("ma_slope_pct"), "close": row.get("close"),
        }
        ev = evaluate_hstech_signal(snap)
        if ev["is_buy"] or pd.Timestamp(row["date"]).day <= 5:
            d = pd.Timestamp(row["date"]).strftime("%Y-%m-%d")
            print(
                f"{d} PE分位={row.get('pe_percentile'):.1f}% "
                f"区间={row.get('year_range_position', 0)*100:.1f}% "
                f"买={'是' if ev['is_buy'] else '否'}"
            )


# ── 通用增强模拟器 ──────────────────────────────────────────


def simulate_enhanced(
    panel,
    start,
    end,
    amount,
    buy_raw_fn,
    sell_raw_fn=None,
    *,
    date_col="date",
    val_col="close",
    max_buys_per_month=999,
    min_buy_interval_days=0,
    sell_cooldown_days=0,
    partial_sell_frac=1.0,
    sell_trend_filter=False,
    ma_col="ma60",
    dividend_mode=False,
):
    sample = _filter_panel(panel, start, end, date_col=date_col)
    if sample.empty:
        return None
    if val_col not in sample.columns:
        val_col = "close"
    if sell_trend_filter and ma_col not in sample.columns and "close" in sample.columns:
        sample = sample.copy()
        sample[ma_col] = sample["close"].rolling(60, min_periods=30).mean()

    latest_price = float(sample.iloc[-1][val_col])
    units = buy_only_units = 0.0
    total_bought = total_sold = 0.0
    buy_count = sell_count = 0
    last_buy_dt = None
    sell_cooldown_until = None
    month_buy_count = {}

    for _, row in sample.iterrows():
        dt = row["_dt"]
        price = float(row[val_col])
        ym = (dt.year, dt.month)

        raw_buy = buy_raw_fn(row)
        raw_sell = sell_raw_fn(row) if sell_raw_fn and units > 0 else False

        if sell_trend_filter and raw_sell:
            ma = row.get(ma_col)
            close = row.get("close")
            if ma is not None and close is not None and close >= ma:
                raw_sell = False

        can_buy = raw_buy
        if can_buy and sell_cooldown_until and dt < sell_cooldown_until:
            can_buy = False
        if can_buy and min_buy_interval_days and last_buy_dt is not None:
            if (dt - last_buy_dt).days < min_buy_interval_days:
                can_buy = False
        if can_buy and max_buys_per_month < 999:
            if month_buy_count.get(ym, 0) >= max_buys_per_month:
                can_buy = False

        if can_buy:
            units += amount / price
            buy_only_units += amount / price
            total_bought += amount
            buy_count += 1
            last_buy_dt = dt
            month_buy_count[ym] = month_buy_count.get(ym, 0) + 1
        elif raw_sell and units > 0:
            frac = partial_sell_frac
            su = units * frac
            total_sold += su * price
            units -= su
            sell_count += 1
            if sell_cooldown_days:
                sell_cooldown_until = dt + pd.Timedelta(days=sell_cooldown_days)
            if units <= 1e-12:
                units = 0.0

    fv = total_sold + units * latest_price
    bov = buy_only_units * latest_price
    ret = (fv - total_bought) / total_bought * 100 if total_bought else None
    bor = (bov - total_bought) / total_bought * 100 if total_bought else None
    return {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "return_pct": ret,
        "buy_only_return_pct": bor,
        "total_bought": total_bought,
        "profit": fv - total_bought,
    }


def tiered_sell_fn(row, code, tiers=None):
    """分级减仓：返回 (triggered, fraction)。"""
    if tiers is None:
        tiers = [
            (80, 30, 0.20),
            (85, 25, 0.20),
            (90, 20, 0.20),
        ]
    pe = row.get("pe_percentile")
    sp = row.get("spread_percentile")
    if pe is None:
        return False, 0
    for pe_min, sp_max, frac in tiers:
        if pe >= pe_min and (sp is None or sp <= sp_max):
            return True, frac
    ev = evaluate_cn_broad_sell(_cn_snap(row, code))
    if ev["is_sell"]:
        return True, 1.0
    return False, 0


def simulate_tiered_cn(panel, code, start, end, amount, **freq_kw):
    """宽基分级减仓 + 频率限制。"""
    sample = _filter_panel(panel, start, end)
    if sample.empty:
        return None
    latest_price = float(sample.iloc[-1]["close"])
    units = buy_only_units = 0.0
    total_bought = total_sold = 0.0
    buy_count = sell_count = 0
    last_buy_dt = None
    sell_cooldown_until = None
    month_buy_count = {}
    max_bpm = freq_kw.get("max_buys_per_month", 999)
    min_int = freq_kw.get("min_buy_interval_days", 0)
    cooldown = freq_kw.get("sell_cooldown_days", 0)
    trend = freq_kw.get("sell_trend_filter", False)
    if trend:
        sample = sample.copy()
        sample["ma60"] = sample["close"].rolling(60, min_periods=30).mean()

    for _, row in sample.iterrows():
        dt = row["_dt"]
        price = float(row["close"])
        ym = (dt.year, dt.month)
        is_buy = evaluate_cn_broad_buy(_cn_snap(row, code))["is_buy"]
        triggered, frac = tiered_sell_fn(row, code)

        if trend and triggered:
            ma = row.get("ma60")
            if ma is not None and row["close"] >= ma:
                triggered = False

        if is_buy:
            if sell_cooldown_until and dt < sell_cooldown_until:
                is_buy = False
            if is_buy and min_int and last_buy_dt and (dt - last_buy_dt).days < min_int:
                is_buy = False
            if is_buy and month_buy_count.get(ym, 0) >= max_bpm:
                is_buy = False
        if is_buy:
            units += amount / price
            buy_only_units += amount / price
            total_bought += amount
            buy_count += 1
            last_buy_dt = dt
            month_buy_count[ym] = month_buy_count.get(ym, 0) + 1
        elif triggered and units > 0:
            su = units * frac
            total_sold += su * price
            units -= su
            sell_count += 1
            if cooldown:
                sell_cooldown_until = dt + pd.Timedelta(days=cooldown)

    fv = total_sold + units * latest_price
    bov = buy_only_units * latest_price
    ret = (fv - total_bought) / total_bought * 100 if total_bought else None
    bor = (bov - total_bought) / total_bought * 100 if total_bought else None
    return {"buy_count": buy_count, "sell_count": sell_count, "return_pct": ret, "buy_only_return_pct": bor, "total_bought": total_bought}


def dividend_sell_fn(row, code):
    """红利卖出：利差分位极低且 PE 分位偏高。"""
    sp = row.get("spread_percentile")
    pe = row.get("pe_percentile")
    spread = row.get("spread")
    if sp is not None and pe is not None:
        if sp <= 15 and pe >= 70:
            return True
    if spread is not None and spread < 0.01 and pe is not None and pe >= 65:
        return True
    return False


def run_portfolio(strategy_fn, panels, amounts):
    """汇总全组合收益。"""
    total_bought = total_profit = 0.0
    total_buys = total_sells = 0
    for r in strategy_fn(panels, amounts):
        total_bought += r["total_bought"]
        total_profit += r.get("profit", 0)
        total_buys += r["buy_count"]
        total_sells += r["sell_count"]
    ret = total_profit / total_bought * 100 if total_bought else None
    return {
        "return_pct": ret,
        "total_bought": total_bought,
        "buy_count": total_buys,
        "sell_count": total_sells,
        "profit": total_profit,
    }


def strategy_baseline(panels, amounts):
    results = []
    for item in INDICES:
        code = item["code"]
        s = simulate_enhanced(
            panels.dividend_panel(code), START, END, amounts["dividend"],
            lambda r, c=code: is_buy_signal_row(r, c), val_col="total_return_close",
        )
        s["code"] = code
        results.append(s)
    for item in CN_BROAD_BACKTEST_INDICES:
        code = item["code"]
        s = simulate_enhanced(
            panels.cn_broad_panel(code), START, END, amounts["cn_broad"],
            lambda r, c=code: _cn_broad_signals(r, c)[0],
            sell_raw_fn=lambda r, c=code: _cn_broad_signals(r, c)[1],
        )
        s["code"] = code
        results.append(s)
    cyb = panels.cyb_panel()
    s = simulate_enhanced(
        cyb, START, END, amounts["other"],
        lambda r: _cyb_signals(r)[0],
        sell_raw_fn=lambda r: _cyb_signals(r)[1], date_col="date",
    )
    s["code"] = CYB_INDEX["code"]
    results.append(s)
    hp = panels.hstech_panel()
    s = simulate_enhanced(
        hp, START, END, amounts["other"],
        lambda r: _hstech_signals(r)[0],
        sell_raw_fn=lambda r: _hstech_signals(r)[1], date_col="date",
    )
    s["code"] = HSTECH_INDEX["code"]
    results.append(s)
    from config import US_INDEX_KEYS
    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        meta = US_INDEX_META[key]
        s = simulate_enhanced(
            daily, START, END, amounts["other"],
            lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g),
        )
        s["code"] = meta["code"]
        results.append(s)
    for r in results:
        r["profit"] = r["total_bought"] * (r["return_pct"] or 0) / 100
    return results


def strategy_buy_limit_4(panels, amounts):
    results = []
    kw = {"max_buys_per_month": 4, "min_buy_interval_days": 3}
    for item in INDICES:
        code = item["code"]
        s = simulate_enhanced(
            panels.dividend_panel(code), START, END, amounts["dividend"],
            lambda r, c=code: is_buy_signal_row(r, c), val_col="total_return_close", **kw,
        )
        s["code"] = code
        results.append(s)
    for item in CN_BROAD_BACKTEST_INDICES:
        code = item["code"]
        s = simulate_enhanced(
            panels.cn_broad_panel(code), START, END, amounts["cn_broad"],
            lambda r, c=code: _cn_broad_signals(r, c)[0],
            sell_raw_fn=lambda r, c=code: _cn_broad_signals(r, c)[1], **kw,
        )
        s["code"] = code
        results.append(s)
    cyb = panels.cyb_panel()
    s = simulate_enhanced(cyb, START, END, amounts["other"], lambda r: _cyb_signals(r)[0],
        sell_raw_fn=lambda r: _cyb_signals(r)[1], date_col="date", **kw)
    s["code"] = CYB_INDEX["code"]
    results.append(s)
    hp = panels.hstech_panel()
    s = simulate_enhanced(hp, START, END, amounts["other"], lambda r: _hstech_signals(r)[0],
        sell_raw_fn=lambda r: _hstech_signals(r)[1], date_col="date", **kw)
    s["code"] = HSTECH_INDEX["code"]
    results.append(s)
    from config import US_INDEX_KEYS
    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        s = simulate_enhanced(daily, START, END, amounts["other"],
            lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g), **kw)
        s["code"] = US_INDEX_META[key]["code"]
        results.append(s)
    for r in results:
        r["profit"] = r["total_bought"] * (r["return_pct"] or 0) / 100
    return results


def strategy_tiered_sell(panels, amounts):
    results = []
    for item in INDICES:
        code = item["code"]
        s = simulate_enhanced(
            panels.dividend_panel(code), START, END, amounts["dividend"],
            lambda r, c=code: is_buy_signal_row(r, c), val_col="total_return_close",
        )
        s["code"] = code
        results.append(s)
    for item in CN_BROAD_BACKTEST_INDICES:
        code = item["code"]
        s = simulate_tiered_cn(panels.cn_broad_panel(code), code, START, END, amounts["cn_broad"])
        s["code"] = code
        results.append(s)
    cyb = panels.cyb_panel()
    s = simulate_enhanced(cyb, START, END, amounts["other"], lambda r: _cyb_signals(r)[0],
        sell_raw_fn=lambda r: _cyb_signals(r)[1], date_col="date")
    s["code"] = CYB_INDEX["code"]
    results.append(s)
    hp = panels.hstech_panel()
    s = simulate_enhanced(hp, START, END, amounts["other"], lambda r: _hstech_signals(r)[0],
        sell_raw_fn=lambda r: _hstech_signals(r)[1], date_col="date")
    s["code"] = HSTECH_INDEX["code"]
    results.append(s)
    from config import US_INDEX_KEYS
    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        s = simulate_enhanced(daily, START, END, amounts["other"],
            lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g))
        s["code"] = US_INDEX_META[key]["code"]
        results.append(s)
    for r in results:
        r["profit"] = r["total_bought"] * (r["return_pct"] or 0) / 100
    return results


def strategy_trend_sell(panels, amounts):
    results = []
    for item in INDICES:
        code = item["code"]
        s = simulate_enhanced(
            panels.dividend_panel(code), START, END, amounts["dividend"],
            lambda r, c=code: is_buy_signal_row(r, c), val_col="total_return_close",
        )
        s["code"] = code
        results.append(s)
    for item in CN_BROAD_BACKTEST_INDICES:
        code = item["code"]
        s = simulate_enhanced(
            panels.cn_broad_panel(code), START, END, amounts["cn_broad"],
            lambda r, c=code: _cn_broad_signals(r, c)[0],
            sell_raw_fn=lambda r, c=code: _cn_broad_signals(r, c)[1],
            sell_trend_filter=True,
        )
        s["code"] = code
        results.append(s)
    cyb = panels.cyb_panel()
    s = simulate_enhanced(cyb, START, END, amounts["other"], lambda r: _cyb_signals(r)[0],
        sell_raw_fn=lambda r: _cyb_signals(r)[1], date_col="date", sell_trend_filter=True)
    s["code"] = CYB_INDEX["code"]
    results.append(s)
    hp = panels.hstech_panel()
    s = simulate_enhanced(hp, START, END, amounts["other"], lambda r: _hstech_signals(r)[0],
        sell_raw_fn=lambda r: _hstech_signals(r)[1], date_col="date")
    s["code"] = HSTECH_INDEX["code"]
    results.append(s)
    from config import US_INDEX_KEYS
    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        s = simulate_enhanced(daily, START, END, amounts["other"],
            lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g))
        s["code"] = US_INDEX_META[key]["code"]
        results.append(s)
    for r in results:
        r["profit"] = r["total_bought"] * (r["return_pct"] or 0) / 100
    return results


def strategy_dividend_sell(panels, amounts):
    """红利增加卖出 + 买入频率限制。"""
    results = []
    kw = {"max_buys_per_month": 4, "min_buy_interval_days": 5}
    for item in INDICES:
        code = item["code"]
        s = simulate_enhanced(
            panels.dividend_panel(code), START, END, amounts["dividend"],
            lambda r, c=code: is_buy_signal_row(r, c),
            sell_raw_fn=lambda r, c=code: dividend_sell_fn(r, c),
            val_col="total_return_close", **kw,
        )
        s["code"] = code
        results.append(s)
    for item in CN_BROAD_BACKTEST_INDICES:
        code = item["code"]
        s = simulate_enhanced(
            panels.cn_broad_panel(code), START, END, amounts["cn_broad"],
            lambda r, c=code: _cn_broad_signals(r, c)[0],
            sell_raw_fn=lambda r, c=code: _cn_broad_signals(r, c)[1],
        )
        s["code"] = code
        results.append(s)
    cyb = panels.cyb_panel()
    s = simulate_enhanced(cyb, START, END, amounts["other"], lambda r: _cyb_signals(r)[0],
        sell_raw_fn=lambda r: _cyb_signals(r)[1], date_col="date")
    s["code"] = CYB_INDEX["code"]
    results.append(s)
    hp = panels.hstech_panel()
    s = simulate_enhanced(hp, START, END, amounts["other"], lambda r: _hstech_signals(r)[0],
        sell_raw_fn=lambda r: _hstech_signals(r)[1], date_col="date")
    s["code"] = HSTECH_INDEX["code"]
    results.append(s)
    from config import US_INDEX_KEYS
    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        s = simulate_enhanced(daily, START, END, amounts["other"],
            lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g))
        s["code"] = US_INDEX_META[key]["code"]
        results.append(s)
    for r in results:
        r["profit"] = r["total_bought"] * (r["return_pct"] or 0) / 100
    return results


def strategy_combo_best(panels, amounts):
    """组合：买入限频 + 宽基分级减仓 + 趋势过滤卖出 + 红利卖出。"""
    results = []
    kw = {"max_buys_per_month": 4, "min_buy_interval_days": 3}
    for item in INDICES:
        code = item["code"]
        s = simulate_enhanced(
            panels.dividend_panel(code), START, END, amounts["dividend"],
            lambda r, c=code: is_buy_signal_row(r, c),
            sell_raw_fn=lambda r, c=code: dividend_sell_fn(r, c),
            val_col="total_return_close",
            max_buys_per_month=4, min_buy_interval_days=5,
        )
        s["code"] = code
        results.append(s)
    for item in CN_BROAD_BACKTEST_INDICES:
        code = item["code"]
        s = simulate_tiered_cn(
            panels.cn_broad_panel(code), code, START, END, amounts["cn_broad"],
            max_buys_per_month=4, min_buy_interval_days=3,
            sell_cooldown_days=30, sell_trend_filter=True,
        )
        s["code"] = code
        results.append(s)
    cyb = panels.cyb_panel()
    s = simulate_enhanced(cyb, START, END, amounts["other"], lambda r: _cyb_signals(r)[0],
        sell_raw_fn=lambda r: _cyb_signals(r)[1], date_col="date",
        sell_trend_filter=True, **kw)
    s["code"] = CYB_INDEX["code"]
    results.append(s)
    hp = panels.hstech_panel()
    s = simulate_enhanced(hp, START, END, amounts["other"], lambda r: _hstech_signals(r)[0],
        sell_raw_fn=lambda r: _hstech_signals(r)[1], date_col="date", **kw)
    s["code"] = HSTECH_INDEX["code"]
    results.append(s)
    from config import US_INDEX_KEYS
    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        s = simulate_enhanced(daily, START, END, amounts["other"],
            lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g), **kw)
        s["code"] = US_INDEX_META[key]["code"]
        results.append(s)
    for r in results:
        r["profit"] = r["total_bought"] * (r["return_pct"] or 0) / 100
    return results


def strategy_buy_only(panels, amounts):
    """纯定投对照：所有模块只买不卖。"""
    results = []
    for item in INDICES:
        code = item["code"]
        s = simulate_enhanced(
            panels.dividend_panel(code), START, END, amounts["dividend"],
            lambda r, c=code: is_buy_signal_row(r, c), val_col="total_return_close",
        )
        s["code"] = code
        results.append(s)
    for item in CN_BROAD_BACKTEST_INDICES:
        code = item["code"]
        s = simulate_enhanced(
            panels.cn_broad_panel(code), START, END, amounts["cn_broad"],
            lambda r, c=code: _cn_broad_signals(r, c)[0],
        )
        s["code"] = code
        results.append(s)
    cyb = panels.cyb_panel()
    s = simulate_enhanced(cyb, START, END, amounts["other"],
        lambda r: _cyb_signals(r)[0], date_col="date")
    s["code"] = CYB_INDEX["code"]
    results.append(s)
    hp = panels.hstech_panel()
    s = simulate_enhanced(hp, START, END, amounts["other"],
        lambda r: _hstech_signals(r)[0], date_col="date")
    s["code"] = HSTECH_INDEX["code"]
    results.append(s)
    from config import US_INDEX_KEYS
    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        s = simulate_enhanced(daily, START, END, amounts["other"],
            lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g))
        s["code"] = US_INDEX_META[key]["code"]
        results.append(s)
    for r in results:
        r["profit"] = r["total_bought"] * (r["return_pct"] or 0) / 100
        r["return_pct"] = r["buy_only_return_pct"]
    return results


def main():
    configure_stdout_utf8()
    diagnose_hs300_2021()
    diagnose_hstech_2022_oct()

    panels = BacktestPanels()
    amounts = resolve_backtest_amounts()

    strategies = [
        ("当前策略(买卖)", strategy_baseline),
        ("纯定投(只买不卖)", strategy_buy_only),
        ("买入限频(月4次/隔3天)", strategy_buy_limit_4),
        ("宽基分级减仓", strategy_tiered_sell),
        ("趋势过滤卖出(跌破MA60)", strategy_trend_sell),
        ("红利加卖出+限频", strategy_dividend_sell),
        ("组合优化", strategy_combo_best),
    ]

    print("\n=== 全组合策略对比 (2016-2025) ===")
    print(f"{'策略':<28} {'总投入':>10} {'总收益':>10} {'收益率':>8} {'买入':>6} {'卖出':>6} {'卖/买':>6}")
    print("-" * 85)

    baseline_ret = None
    for name, fn in strategies:
        p = run_portfolio(fn, panels, amounts)
        ratio = p["sell_count"] / p["buy_count"] * 100 if p["buy_count"] else 0
        if name.startswith("当前"):
            baseline_ret = p["return_pct"]
        delta = f" ({p['return_pct'] - baseline_ret:+.1f}pp)" if baseline_ret and not name.startswith("当前") else ""
        print(
            f"{name:<28} {p['total_bought']:>10,.0f} {p['profit']:>+10,.0f} "
            f"{p['return_pct']:>+7.1f}%{delta:<8} {p['buy_count']:>6} {p['sell_count']:>6} {ratio:>5.1f}%"
        )

    # 单指数：沪深300各方案
    print("\n=== 沪深300 单指数方案对比 ===")
    panel = panels.cn_broad_panel("000300")
    amt = amounts["cn_broad"]
    bl = simulate_enhanced(panel, START, END, amt,
        lambda r: _cn_broad_signals(r, "000300")[0],
        sell_raw_fn=lambda r: _cn_broad_signals(r, "000300")[1])
    variants = [
        ("当前", bl),
        ("分级减仓", simulate_tiered_cn(panel, "000300", START, END, amt)),
        ("趋势过滤卖", simulate_enhanced(panel, START, END, amt,
            lambda r: _cn_broad_signals(r, "000300")[0],
            sell_raw_fn=lambda r: _cn_broad_signals(r, "000300")[1], sell_trend_filter=True)),
        ("限频+分级+趋势", simulate_tiered_cn(panel, "000300", START, END, amt,
            max_buys_per_month=4, min_buy_interval_days=3, sell_trend_filter=True)),
        ("只买不卖", simulate_enhanced(panel, START, END, amt,
            lambda r: _cn_broad_signals(r, "000300")[0])),
    ]
    print(f"{'方案':<20} {'买入':>5} {'卖出':>5} {'收益率':>8} {'仅买':>8}")
    for name, s in variants:
        print(f"{name:<20} {s['buy_count']:>5} {s['sell_count']:>5} "
              f"{s['return_pct']:>+7.1f}% {s['buy_only_return_pct']:>+7.1f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
