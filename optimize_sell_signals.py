"""为沪深300/科创50/创业板搜索卖点阈值（对比仅买入持有）。"""

import sys
from itertools import product

from backtest_buy_signals import BacktestPanels
from backtest_trade_signals import _filter_panel
from buy_amount_config import resolve_simulate_amount
from cn_broad_signal import evaluate_cn_broad_buy
from config import (
    CYB_HISTORICAL_GROWTH,
    get_backtest_buy_amount,
    get_cn_broad_signal_config,
    resolve_backtest_amounts,
)
from cyb_signal import evaluate_cyb_signal, compute_peg
from market_data import configure_stdout_utf8
from price_position import price_position_sell_hit

START = "2015-01-01"


def _cn_snap(row, code):
    return {
        "code": code,
        "pe_percentile": row.get("pe_percentile"),
        "pb_percentile": row.get("pb_percentile"),
        "spread_percentile": row.get("spread_percentile"),
        "pct_above_low": row.get("pct_above_low"),
        "pct_below_high": row.get("pct_below_high"),
        "year_range_position": row.get("year_range_position"),
        "ma_slope_pct": row.get("ma_slope_pct"),
    }


def _cyb_snap(row):
    return {
        "pe": row["pe"],
        "pb": row["pb"],
        "pe_percentile": row.get("pe_percentile"),
        "pb_percentile": row.get("pb_percentile"),
        "pct_above_low": row.get("pct_above_low"),
        "pct_below_high": row.get("pct_below_high"),
        "year_range_position": row.get("year_range_position"),
        "ma_slope_pct": row.get("ma_slope_pct"),
    }


def simulate_with_gain(
    panel,
    buy_fn,
    sell_fn,
    amount,
    start=START,
    end=None,
):
    """支持持仓成本与浮盈判断的波段模拟。"""
    sample = _filter_panel(panel, start, end)
    if sample.empty:
        return None

    latest_price = float(sample.iloc[-1]["close"])
    units = buy_only_units = 0.0
    total_bought = total_sold = 0.0
    cost_basis = 0.0
    buy_count = sell_count = 0
    buy_dates = []
    sell_dates = []

    for _, row in sample.iterrows():
        price = float(row["close"])
        day = row["_dt"].strftime("%Y-%m-%d")
        gain = (units * price - cost_basis) / cost_basis if cost_basis > 0 else 0.0
        is_buy = buy_fn(row)
        is_sell = sell_fn(row, gain) if units > 0 else False

        buy_amount = float(amount(row)) if callable(amount) else float(amount)
        if is_buy and buy_amount > 0:
            units += buy_amount / price
            buy_only_units += buy_amount / price
            total_bought += buy_amount
            cost_basis += buy_amount
            buy_count += 1
            buy_dates.append(day)
        elif is_sell and units > 0:
            total_sold += units * price
            units = 0.0
            cost_basis = 0.0
            sell_count += 1
            sell_dates.append(day)

    final_value = total_sold + units * latest_price
    buy_only_value = buy_only_units * latest_price
    profit = final_value - total_bought
    buy_only_profit = buy_only_value - total_bought
    return_pct = profit / total_bought * 100 if total_bought > 0 else None
    buy_only_return_pct = (
        buy_only_profit / total_bought * 100 if total_bought > 0 else None
    )
    return {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_bought": total_bought,
        "return_pct": return_pct,
        "buy_only_return_pct": buy_only_return_pct,
        "profit": profit,
        "buy_only_profit": buy_only_profit,
        "sell_dates": sell_dates,
        "buy_dates": buy_dates,
    }


def _cn_valuation_sell(snap, cfg):
    pe = snap.get("pe_percentile")
    sp = snap.get("spread_percentile")
    al = snap.get("pct_above_low")
    yr = snap.get("year_range_position")
    pe_hit = pe is not None and pe >= cfg["sell_pe_percentile_min"]
    spread_hit = sp is not None and sp <= cfg["sell_spread_percentile_max"]
    price_hit = price_position_sell_hit(al, cfg["sell_max_above_low_pct"])
    yr_hit = yr is not None and yr >= cfg.get("sell_year_range_min", 1.0)
    confirms = [spread_hit, price_hit]
    if cfg.get("sell_year_range_min", 1.0) < 1.0:
        confirms.append(yr_hit)
    return pe_hit and any(confirms)


def _cn_peak_sell(snap, cfg):
    yr = snap.get("year_range_position")
    bh = snap.get("pct_below_high")
    al = snap.get("pct_above_low")
    if yr is None or bh is None:
        return False
    near_high = bh <= cfg.get("sell_peak_max_below_high_pct", 0.05)
    yr_ok = yr >= cfg.get("sell_peak_year_range_min", 0.90)
    al_ok = al is None or al >= cfg.get("sell_peak_min_above_low_pct", 0.20)
    return near_high and yr_ok and al_ok


def _cn_gain_sell(gain, snap, cfg):
    if gain < cfg.get("sell_min_unrealized_gain_pct", 1.0):
        return False
    yr = snap.get("year_range_position")
    pe = snap.get("pe_percentile")
    yr_ok = yr is not None and yr >= cfg.get("sell_gain_year_range_min", 0.80)
    pe_ok = pe is None or pe >= cfg.get("sell_gain_pe_percentile_min", 0.0)
    return yr_ok and pe_ok


def make_cn_sell_fn(code, cfg):
    def sell_fn(row, gain):
        snap = _cn_snap(row, code)
        if evaluate_cn_broad_buy(snap)["is_buy"]:
            return False
        return (
            _cn_valuation_sell(snap, cfg)
            or _cn_peak_sell(snap, cfg)
            or _cn_gain_sell(gain, snap, cfg)
        )

    return sell_fn


def _cyb_valuation_sell(snap, cfg):
    pe = snap.get("pe_percentile")
    pb = snap.get("pb_percentile")
    peg = compute_peg(snap.get("pe"), CYB_HISTORICAL_GROWTH)
    if pe is not None and pb is not None:
        if pe >= cfg["sell_pe_percentile_min"] and pb >= cfg["sell_pb_percentile_min"]:
            return True
    if peg is not None and pe is not None and pb is not None:
        if (
            peg >= cfg.get("sell_peg_hist_min", 99)
            and pe >= cfg.get("sell_combo_pe_percentile_min", 99)
            and pb >= cfg.get("sell_combo_pb_percentile_min", 99)
        ):
            return True
    return False


def _cyb_price_sell(snap, cfg):
    pe = snap.get("pe_percentile")
    al = snap.get("pct_above_low")
    if pe is None or al is None:
        return False
    return (
        pe >= cfg.get("sell_price_pe_percentile_min", 75)
        and al >= cfg.get("sell_price_min_above_low_pct", 0.30)
    )


def _cyb_peak_sell(snap, cfg):
    yr = snap.get("year_range_position")
    bh = snap.get("pct_below_high")
    al = snap.get("pct_above_low")
    if yr is None or bh is None:
        return False
    return (
        yr >= cfg.get("sell_peak_year_range_min", 0.90)
        and bh <= cfg.get("sell_peak_max_below_high_pct", 0.05)
        and (al is None or al >= cfg.get("sell_peak_min_above_low_pct", 0.25))
    )


def _cyb_gain_sell(gain, snap, cfg):
    if gain < cfg.get("sell_min_unrealized_gain_pct", 1.0):
        return False
    pe = snap.get("pe_percentile")
    pb = snap.get("pb_percentile")
    yr = snap.get("year_range_position")
    val_ok = (
        (pe is not None and pe >= cfg.get("sell_gain_pe_percentile_min", 65))
        or (pb is not None and pb >= cfg.get("sell_gain_pb_percentile_min", 70))
    )
    yr_ok = yr is None or yr >= cfg.get("sell_gain_year_range_min", 0.75)
    return val_ok and yr_ok


def make_cyb_sell_fn(cfg):
    def sell_fn(row, gain):
        snap = _cyb_snap(row)
        if evaluate_cyb_signal(snap)["is_buy"]:
            return False
        return (
            _cyb_valuation_sell(snap, cfg)
            or _cyb_price_sell(snap, cfg)
            or _cyb_peak_sell(snap, cfg)
            or _cyb_gain_sell(gain, snap, cfg)
        )

    return sell_fn


def _resolve_amount(code, panel, amounts, buy_fn):
    base = get_backtest_buy_amount(code, amounts)
    return resolve_simulate_amount(code, base, amounts, panel, START, None, buy_fn)


def search_cn(code, panel, amounts):
    pre_base = get_cn_broad_signal_config(code)
    buy_fn = lambda r, c=code: evaluate_cn_broad_buy(_cn_snap(r, c))["is_buy"]
    sim_amt = _resolve_amount(code, panel, amounts, buy_fn)
    hold = simulate_with_gain(
        panel, buy_fn, lambda r, g: False, sim_amt, start=START
    )
    results = []
    for pe_min, sp_max, above_low, yr_min, peak_yr, peak_bh, min_gain in product(
        [82, 85, 88],
        [20, 25, 30],
        [0.20, 0.25, 0.30],
        [0.80, 0.85, 1.0],
        [0.88, 0.92, 0.95],
        [0.03, 0.05, 0.08],
        [0.50, 0.70, 0.90],
    ):
        cfg = {
            **pre_base,
            "sell_pe_percentile_min": pe_min,
            "sell_spread_percentile_max": sp_max,
            "sell_max_above_low_pct": above_low,
            "sell_year_range_min": yr_min,
            "sell_peak_year_range_min": peak_yr,
            "sell_peak_max_below_high_pct": peak_bh,
            "sell_peak_min_above_low_pct": 0.20,
            "sell_min_unrealized_gain_pct": min_gain,
            "sell_gain_year_range_min": 0.80,
            "sell_gain_pe_percentile_min": 70,
        }
        stats = simulate_with_gain(
            panel, buy_fn, make_cn_sell_fn(code, cfg), sim_amt, start=START
        )
        if not stats or stats["sell_count"] < 1:
            continue
        delta = (stats["return_pct"] or 0) - (stats["buy_only_return_pct"] or 0)
        results.append((delta, stats, cfg))
    results.sort(key=lambda x: x[0], reverse=True)
    return hold, results


def search_cyb(panel, amounts):
    buy_fn = lambda r: evaluate_cyb_signal(_cyb_snap(r))["is_buy"]
    sim_amt = _resolve_amount("399006", panel, amounts, buy_fn)
    hold = simulate_with_gain(
        panel, buy_fn, lambda r, g: False, sim_amt, start=START
    )
    results = []
    for pe_min, pb_min, price_pe, price_al, peak_yr, peak_bh, min_gain in product(
        [75, 78, 80],
        [72, 75, 78],
        [70, 75, 78],
        [0.25, 0.30, 0.35],
        [0.88, 0.92, 0.95],
        [0.03, 0.05, 0.08],
        [0.50, 0.70, 0.90],
    ):
        cfg = {
            "sell_pe_percentile_min": pe_min,
            "sell_pb_percentile_min": pb_min,
            "sell_peg_hist_min": 3.0,
            "sell_combo_pe_percentile_min": 60,
            "sell_combo_pb_percentile_min": 60,
            "sell_price_pe_percentile_min": price_pe,
            "sell_price_min_above_low_pct": price_al,
            "sell_peak_year_range_min": peak_yr,
            "sell_peak_max_below_high_pct": peak_bh,
            "sell_peak_min_above_low_pct": 0.25,
            "sell_min_unrealized_gain_pct": min_gain,
            "sell_gain_pe_percentile_min": 65,
            "sell_gain_pb_percentile_min": 70,
            "sell_gain_year_range_min": 0.75,
        }
        stats = simulate_with_gain(
            panel, buy_fn, make_cyb_sell_fn(cfg), sim_amt, start=START
        )
        if not stats or stats["sell_count"] < 1:
            continue
        delta = (stats["return_pct"] or 0) - (stats["buy_only_return_pct"] or 0)
        results.append((delta, stats, cfg))
    results.sort(key=lambda x: x[0], reverse=True)
    return hold, results


def main():
    configure_stdout_utf8()
    panels = BacktestPanels()
    amounts = resolve_backtest_amounts(tier_enabled=True)

    print(f"=== 卖点参数搜索（{START} 至今，含分档金额）===\n")

    for code, name in (("000300", "沪深300"), ("000688", "科创50")):
        panel = panels.cn_broad_panel(code)
        hold, top = search_cn(code, panel, amounts)
        print(
            f"[{code} {name}] 仅买入: {hold['buy_only_return_pct']:+.1f}% "
            f"利润 {hold['buy_only_profit']:+.0f} 买入 {hold['buy_count']} 次"
        )
        if not top:
            print("  未找到有效卖点组合\n")
            continue
        for i, (delta, stats, cfg) in enumerate(top[:5]):
            print(
                f"  #{i+1} 超额 {delta:+.1f}% | 波段 {stats['return_pct']:+.1f}% "
                f"vs 持有 {stats['buy_only_return_pct']:+.1f}% | "
                f"卖 {stats['sell_count']} 次 {stats['sell_dates']}"
            )
            print(
                f"      估值 PE≥{cfg['sell_pe_percentile_min']:.0f} "
                f"利差≤{cfg['sell_spread_percentile_max']:.0f} "
                f"距低点≥{cfg['sell_max_above_low_pct']*100:.0f}% "
                f"年区间≥{cfg['sell_year_range_min']*100:.0f}% | "
                f"峰值 年区间≥{cfg['sell_peak_year_range_min']*100:.0f}% "
                f"回撤≤{cfg['sell_peak_max_below_high_pct']*100:.0f}% | "
                f"浮盈≥{cfg['sell_min_unrealized_gain_pct']*100:.0f}%"
            )
        print()

    cyb_panel = panels.cyb_panel()
    hold, cyb_top = search_cyb(cyb_panel, amounts)
    print(
        f"[399006 创业板] 仅买入: {hold['buy_only_return_pct']:+.1f}% "
        f"利润 {hold['buy_only_profit']:+.0f} 买入 {hold['buy_count']} 次"
    )
    for i, (delta, stats, cfg) in enumerate(cyb_top[:5]):
        print(
            f"  #{i+1} 超额 {delta:+.1f}% | 波段 {stats['return_pct']:+.1f}% "
            f"vs 持有 {stats['buy_only_return_pct']:+.1f}% | "
            f"卖 {stats['sell_count']} 次 {stats['sell_dates']}"
        )
        print(
            f"      估值 PE≥{cfg['sell_pe_percentile_min']:.0f} "
            f"PB≥{cfg['sell_pb_percentile_min']:.0f} | "
            f"价格 PE≥{cfg['sell_price_pe_percentile_min']:.0f} "
            f"距低点≥{cfg['sell_price_min_above_low_pct']*100:.0f}% | "
            f"峰值 年区间≥{cfg['sell_peak_year_range_min']*100:.0f}% | "
            f"浮盈≥{cfg['sell_min_unrealized_gain_pct']*100:.0f}%"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
