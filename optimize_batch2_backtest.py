"""第二批策略建议回测对比（2016-2025）。"""

import sys

import pandas as pd

from backtest_buy_signals import BacktestPanels, CN_BROAD_BACKTEST_INDICES, US_INDEX_META, _us_buy_snapshot
from backtest_trade_signals import (
    _filter_panel,
    simulate_trades,
)
from cn_broad_data import attach_cn_broad_percentiles, build_cn_broad_valuation_history
from cn_broad_signal import evaluate_cn_broad_buy, evaluate_cn_broad_sell
from config import (
    CYB_HISTORICAL_GROWTH,
    CYB_INDEX,
    HSTECH_HISTORICAL_GROWTH,
    HSTECH_INDEX,
    INDICES,
    US_INDEX_KEYS,
    get_dividend_signal_config,
    resolve_backtest_amounts,
)
from cyb_signal import compute_peg, evaluate_cyb_signal
from dividend_data import is_buy_signal, is_buy_signal_row
from hstech_signal import evaluate_hstech_signal
from market_data import configure_stdout_utf8, get_gov_bond_yield_history
from optimize_strategy_backtest import (
    END,
    START,
    _attach_pe_growth_1y,
    _conservative_growth,
    _cn_snap,
    baseline_cn_broad,
    baseline_cyb,
    baseline_dividend_buy,
    simulate_partial_sell,
)

# ── 恒科：动态卖出（替代成本+25%）────────────────────────


def _hstech_signals_simple(row):
    snap = {
        "pe": row["pe"],
        "pe_percentile": row.get("pe_percentile"),
        "dividend_percentile": row.get("dividend_percentile"),
        "pct_above_low": row.get("pct_above_low"),
        "pct_below_high": row.get("pct_below_high"),
        "year_range_position": row.get("year_range_position"),
        "ma_slope_pct": row.get("ma_slope_pct"),
        "close": row.get("close"),
    }
    ev = evaluate_hstech_signal(snap)
    return ev["is_buy"], ev.get("is_sell", False)


def hstech_dynamic_sell_row(row, pe_min=78, peg_min=3.0, above_low_min=0.40):
    pe_pct = row.get("pe_percentile")
    peg = compute_peg(row.get("pe"), HSTECH_HISTORICAL_GROWTH)
    above_low = row.get("pct_above_low")
    pe_high = pe_pct is not None and pe_pct >= pe_min
    peg_high = peg is not None and peg >= peg_min
    momentum = above_low is not None and above_low >= above_low_min
    return pe_high and (peg_high or momentum)


def simulate_hstech_dynamic(panel, start, end, amount, date_col="date"):
    sample = _filter_panel(panel, start, end, date_col=date_col)
    if sample.empty:
        return None
    latest_price = float(sample.iloc[-1]["close"])
    units = buy_only_units = 0.0
    total_bought = total_sold = 0.0
    buy_count = sell_count = 0
    for _, row in sample.iterrows():
        price = float(row["close"])
        snap = {
            "pe": row["pe"],
            "pe_percentile": row.get("pe_percentile"),
            "dividend_percentile": row.get("dividend_percentile"),
            "pct_above_low": row.get("pct_above_low"),
            "pct_below_high": row.get("pct_below_high"),
            "year_range_position": row.get("year_range_position"),
            "ma_slope_pct": row.get("ma_slope_pct"),
            "close": price,
        }
        is_buy = evaluate_hstech_signal(snap)["is_buy"]
        is_sell = hstech_dynamic_sell_row(row) and units > 0
        if is_buy:
            units += amount / price
            buy_only_units += amount / price
            total_bought += amount
            buy_count += 1
        elif is_sell:
            total_sold += units * price
            units = 0.0
            sell_count += 1
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
    }


# ── 宽基：右侧止盈 + PE 回撤止损 ──────────────────────────


def simulate_cn_broad_enhanced(
    panel,
    code,
    start,
    end,
    amount,
    profit_pct=0.25,
    profit_pe=80,
    sell_frac=0.30,
    pe_drawdown_pp=15,
):
    sample = _filter_panel(panel, start, end)
    if sample.empty:
        return None
    latest_price = float(sample.iloc[-1]["close"])
    units = buy_only_units = 0.0
    total_bought = total_sold = 0.0
    cost_basis = 0.0
    peak_pe = None
    buy_count = sell_count = 0

    for _, row in sample.iterrows():
        price = float(row["close"])
        is_buy, is_sell_base = baseline_cn_broad(row, code)
        pe_pct = row.get("pe_percentile")
        partial = None

        if units > 0 and pe_pct is not None:
            peak_pe = pe_pct if peak_pe is None else max(peak_pe, pe_pct)
            avg_cost = cost_basis / units
            float_profit = (price - avg_cost) / avg_cost if avg_cost > 0 else 0
            if float_profit >= profit_pct and pe_pct >= profit_pe:
                partial = sell_frac
            elif peak_pe - pe_pct >= pe_drawdown_pp:
                partial = 1.0

        if is_buy:
            units += amount / price
            buy_only_units += amount / price
            cost_basis += amount
            total_bought += amount
            buy_count += 1
            peak_pe = pe_pct
        elif is_sell_base and units > 0:
            total_sold += units * price
            units = cost_basis = 0.0
            peak_pe = None
            sell_count += 1
        elif partial and units > 0:
            su = units * partial
            total_sold += su * price
            cost_basis *= 1 - partial
            units -= su
            sell_count += 1
            if units <= 1e-12:
                units = cost_basis = 0.0
                peak_pe = None

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
    }


def cn_broad_sell_or(row, code, pe_min=80, spread_max=25, above_low_min=0.15):
    """卖出改为 OR：PE 偏高 或（利差收敛且涨幅大）。"""
    ev = evaluate_cn_broad_sell(_cn_snap(row, code))
    pe_pct = row.get("pe_percentile")
    spread_pct = row.get("spread_percentile")
    above_low = row.get("pct_above_low")
    pe_hit = pe_pct is not None and pe_pct >= pe_min
    combo = (
        spread_pct is not None
        and spread_pct <= spread_max
        and above_low is not None
        and above_low >= above_low_min
    )
    is_buy = evaluate_cn_broad_buy(_cn_snap(row, code))["is_buy"]
    return is_buy, (pe_hit or combo) and not is_buy


# ── 红利：放宽价格位置 ────────────────────────────────────


def dividend_buy_relaxed(row, code, above_low_max=None, year_range_max=None):
    spread = row.get("spread")
    spread_pct = row.get("spread_percentile")
    pe_pct = row.get("pe_percentile")
    cfg = get_dividend_signal_config(code)
    overrides = {}
    if above_low_max is not None:
        overrides["buy_max_above_low_pct"] = above_low_max
    if year_range_max is not None:
        overrides["buy_max_year_range_pct"] = year_range_max
    cfg = {**cfg, **overrides}
    return is_buy_signal(
        spread,
        spread_pct,
        pe_pct,
        code,
        pct_above_low=row.get("pct_above_low"),
        pct_below_high=row.get("pct_below_high"),
        year_range_position=row.get("year_range_position"),
    )


# ── 美股：极端估值减仓 ────────────────────────────────────


def us_extreme_sell_fn(row, pe_min=95, above_low_min=None, reduce_frac=0.5):
    pe_pct = row.get("forward_pe_percentile") or row.get("trailing_pe_percentile")
    above_low = row.get("pct_above_low")
    if pe_pct is None:
        return False
    if pe_pct < pe_min:
        return False
    if above_low_min is not None:
        return above_low is not None and above_low >= above_low_min
    return True


def _stats(panel, start, end, amount, buy_fn, sell_fn=None, partial=False, frac=0.5, **kw):
    if partial:
        return simulate_partial_sell(
            panel, start, end, amount, buy_fn, sell_fn, sell_fraction=frac, **kw
        )
    has_sell = kw.pop("has_sell", bool(sell_fn))
    s = simulate_trades(
        panel, start, end, amount=amount, buy_fn=buy_fn, sell_fn=sell_fn,
        has_sell=has_sell, **kw
    )
    if not s:
        return None
    return {
        "buy_count": s["buy_count"],
        "sell_count": s["sell_count"],
        "return_pct": s["return_pct"],
        "buy_only_return_pct": s["buy_only_return_pct"],
        "total_bought": s["total_bought"],
    }


def _print_row(name, mod, code, var, bl):
    if not var or not bl:
        return
    bd = var["buy_count"] - bl["buy_count"]
    bp = bd / bl["buy_count"] * 100 if bl["buy_count"] else 0
    vr = var.get("return_pct") or var.get("buy_only_return_pct") or 0
    br = bl.get("return_pct") or bl.get("buy_only_return_pct") or 0
    dd = vr - br
    if dd >= 2 and bp >= -30:
        v = "✅"
    elif dd < -1 or bp < -40:
        v = "❌"
    else:
        v = "➖"
    print(
        f"{name:<32} {mod:<4} {code:<8} {bd:+4d}({bp:+.0f}%) "
        f"{vr:>+7.1f}% {br:>+7.1f}% {dd:>+7.1f}pp {v}"
    )


def main():
    configure_stdout_utf8()
    print(f"第二批建议回测 {START} ~ {END}\n")
    panels = BacktestPanels()
    amounts = resolve_backtest_amounts()
    bond = get_gov_bond_yield_history()

    print(f"{'优化项':<32} {'模块':<4} {'代码':<8} {'买次Δ':>7} {'收益率':>8} {'基线':>8} {'Δ收益':>8} 判定")
    print("-" * 105)

    # ── 恒科 ──
    hp = panels.hstech_panel()
    bl_hs = _stats(
        hp, START, END, amounts["other"],
        lambda r: _hstech_signals_simple(r)[0],
        sell_fn=lambda r: _hstech_signals_simple(r)[1],
        has_sell=True, date_col="date",
    )
    var_dyn = simulate_hstech_dynamic(hp, START, END, amounts["other"], date_col="date")
    _print_row("恒科动态卖出(PE+PEG/涨幅)", "恒科", "HSTECH", var_dyn, bl_hs)

    # ── 宽基 ──
    for item in CN_BROAD_BACKTEST_INDICES:
        code = item["code"]
        panel = panels.cn_broad_panel(code)
        bl = _stats(
            panel, START, END, amounts["cn_broad"],
            lambda r, c=code: baseline_cn_broad(r, c)[0],
            sell_fn=lambda r, c=code: baseline_cn_broad(r, c)[1],
            has_sell=True,
        )
        var_enh = simulate_cn_broad_enhanced(panel, code, START, END, amounts["cn_broad"])
        _print_row("宽基右侧止盈+PE回撤止损", "宽基", code, var_enh, bl)

        var_or = _stats(
            panel, START, END, amounts["cn_broad"],
            lambda r, c=code: cn_broad_sell_or(r, c)[0],
            sell_fn=lambda r, c=code: cn_broad_sell_or(r, c)[1],
            has_sell=True,
        )
        _print_row("宽基卖出改OR逻辑", "宽基", code, var_or, bl)

        if code == "000688":
            raw = build_cn_broad_valuation_history("000688", bond_history=bond)
            p5y = attach_cn_broad_percentiles(raw, "000688", window=1260, min_days=60)
            var5y = _stats(
                p5y, START, END, amounts["cn_broad"],
                lambda r: baseline_cn_broad(r, "000688")[0],
                sell_fn=lambda r: baseline_cn_broad(r, "000688")[1],
                has_sell=True,
            )
            _print_row("科创50分位窗口5年", "宽基", code, var5y, bl)

    # ── 红利 ──
    relax_map = {
        "930955": (0.08, 0.65),
        "H30269": (0.08, 0.65),
    }
    for item in INDICES:
        code = item["code"]
        panel = panels.dividend_panel(code)
        bl = _stats(
            panel, START, END, amounts["dividend"],
            lambda r, c=code: baseline_dividend_buy(r, c),
            valuation_price_col="total_return_close",
        )
        al, yr = relax_map.get(code, (0.08, 0.65))
        var = _stats(
            panel, START, END, amounts["dividend"],
            lambda r, c=code, a=al, y=yr: dividend_buy_relaxed(r, c, a, y),
            valuation_price_col="total_return_close",
        )
        _print_row("红利放宽价格位置(8%/65%)", "红利", code, var, bl)

    # ── 创业板保守PEG ──
    cyb = _attach_pe_growth_1y(panels.cyb_panel(), date_col="date")
    bl_cyb = _stats(
        cyb, START, END, amounts["other"],
        lambda r: baseline_cyb(r)[0],
        sell_fn=lambda r: baseline_cyb(r)[1],
        has_sell=True, date_col="date",
    )

    def cyb_conservative(r):
        g = _conservative_growth(CYB_HISTORICAL_GROWTH, r.get("growth_1y"))
        peg = compute_peg(r.get("pe"), g)
        ev = evaluate_cyb_signal({"pe": r["pe"], "pb": r["pb"], **r.to_dict()})
        if not ev["is_buy"]:
            return False
        return peg is None or peg <= 2.5

    var_cyb = _stats(
        cyb, START, END, amounts["other"], cyb_conservative,
        sell_fn=lambda r: baseline_cyb(r)[1], has_sell=True, date_col="date",
    )
    _print_row("创业板保守PEG", "创业板", CYB_INDEX["code"], var_cyb, bl_cyb)

    # ── 美股 ──
    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        us = _attach_pe_growth_1y(daily)
        meta = US_INDEX_META[key]
        code = meta["code"]
        bl = _stats(
            us, START, END, amounts["other"],
            lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g),
        )
        for label, fn, frac in [
            ("美股PE≥95%减仓50%", lambda r: us_extreme_sell_fn(r, 95), 0.5),
            ("美股PE≥95%且涨幅≥50%减仓50%", lambda r: us_extreme_sell_fn(r, 95, 0.50), 0.5),
            ("美股PE>80%利率>70%减仓50%", lambda r: (
                (r.get("forward_pe_percentile") or r.get("trailing_pe_percentile") or 0) > 80
                and (r.get("us10y_percentile") or 0) > 70
            ), 0.5),
        ]:
            var = _stats(
                us, START, END, amounts["other"],
                lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g),
                sell_fn=fn, partial=True, frac=frac,
            )
            _print_row(label, "美股", code, var, bl)

    print("-" * 105)
    return 0


if __name__ == "__main__":
    sys.exit(main())
