"""分析回测买入质量：高位买入与低位漏买，输出诊断报告。"""

import argparse
import sys
from collections import defaultdict

import pandas as pd

from backtest_buy_signals import (
    CN_BROAD_BACKTEST_INDICES,
    _iter_backtest_configs,
    get_panels,
)
from cn_broad_signal import evaluate_cn_broad_buy
from config import get_dividend_signal_config
from cyb_signal import evaluate_cyb_signal
from dividend_data import is_buy_signal_row
from hstech_signal import evaluate_hstech_signal
from market_data import configure_stdout_utf8
from ndx_signal import evaluate_ndx_signal, resolve_ndx_expected_growth
from spx_signal import evaluate_spx_signal, resolve_spx_expected_growth

CN_BROAD_CODES = {item["code"] for item in CN_BROAD_BACKTEST_INDICES}
DIVIDEND_CODES = {"930955", "H30269"}


def _year_range_position(close, year_low, year_high):
    if year_low is None or year_high is None or year_high <= year_low:
        return None
    return (close - year_low) / (year_high - year_low)


def _near_year_low(close, year_low, threshold=0.03):
    if year_low is None or year_low <= 0:
        return False
    return close <= year_low * (1 + threshold)


def _diagnose_cn_broad(row, code):
    ev = evaluate_cn_broad_buy(
        {
            "code": code,
            "pe_percentile": row.get("pe_percentile"),
            "pb_percentile": row.get("pb_percentile"),
            "dividend_percentile": row.get("dividend_percentile"),
            "spread_percentile": row.get("spread_percentile"),
            "pct_above_low": row.get("pct_above_low"),
            "pct_below_high": row.get("pct_below_high"),
            "year_range_position": row.get("year_range_position"),
        }
    )
    failed = [c["name"] for c in ev.get("criteria", []) if c.get("applicable") and not c.get("passed")]
    return ev["is_buy"], failed


def _diagnose_dividend(row, code):
    ok = is_buy_signal_row(row, code)
    failed = []
    if not ok:
        cfg = get_dividend_signal_config(code)
        if row.get("spread") is None or row.get("spread") <= cfg["buy_spread_min"]:
            failed.append("利差")
        if row.get("spread_percentile") is None or row.get("spread_percentile") < cfg["buy_spread_percentile_min"]:
            failed.append("利差分位")
        if row.get("pe_percentile") is None or row.get("pe_percentile") > cfg["buy_pe_percentile_max"]:
            failed.append("PE分位")
        if row.get("pct_above_low") is not None and row.get("pct_above_low") > cfg.get("buy_max_above_low_pct", 1):
            failed.append("距低点涨幅")
        if row.get("pct_below_high") is not None and row.get("pct_below_high") < cfg.get("buy_min_drawdown_from_high_pct", 0):
            failed.append("距高点回撤")
        yr = row.get("year_range_position")
        if yr is not None and not pd.isna(yr) and yr > cfg.get("buy_max_year_range_pct", 1):
            failed.append("近1年区间位置")
    return ok, failed


def _diagnose_cyb(row):
    ev = evaluate_cyb_signal(
        {
            "pe": row.get("pe"),
            "pb": row.get("pb"),
            "pe_percentile": row.get("pe_percentile"),
            "pb_percentile": row.get("pb_percentile"),
            "pct_above_low": row.get("pct_above_low"),
            "pct_below_high": row.get("pct_below_high"),
            "year_range_position": row.get("year_range_position"),
        }
    )
    failed = [c["name"] for c in ev.get("criteria", []) if c.get("applicable") and not c.get("passed")]
    return ev["is_buy"], failed


def _diagnose_hstech(row):
    ev = evaluate_hstech_signal(
        {
            "pe": row.get("pe"),
            "pe_percentile": row.get("pe_percentile"),
            "dividend_percentile": row.get("dividend_percentile"),
            "pct_above_low": row.get("pct_above_low"),
            "pct_below_high": row.get("pct_below_high"),
            "year_range_position": row.get("year_range_position"),
        }
    )
    failed = [c["name"] for c in ev.get("criteria", []) if c.get("applicable") and not c.get("passed")]
    return ev["is_buy"], failed


def _diagnose_ndx(row, growth):
    snap = {
        "forward_pe": row.get("forward_pe"),
        "trailing_pe": row.get("trailing_pe"),
        "forward_pe_percentile": row.get("forward_pe_percentile"),
        "trailing_pe_percentile": row.get("trailing_pe_percentile"),
        "us10y_percentile": row.get("us10y_percentile"),
        "implied_growth": row.get("implied_growth"),
        "historical_growth": growth,
        "pct_above_low": row.get("pct_above_low"),
        "pct_below_high": row.get("pct_below_high"),
        "year_range_position": row.get("year_range_position"),
    }
    snap["expected_growth"] = resolve_ndx_expected_growth(snap)
    ev = evaluate_ndx_signal(snap)
    failed = [c["name"] for c in ev.get("criteria", []) if c.get("applicable") and not c.get("passed")]
    return ev["is_buy"], failed


def _diagnose_spx(row, growth):
    snap = {
        "forward_pe": row.get("forward_pe"),
        "trailing_pe": row.get("trailing_pe"),
        "forward_pe_percentile": row.get("forward_pe_percentile"),
        "trailing_pe_percentile": row.get("trailing_pe_percentile"),
        "us10y_percentile": row.get("us10y_percentile"),
        "implied_growth": row.get("implied_growth"),
        "historical_growth": growth,
        "pct_above_low": row.get("pct_above_low"),
        "pct_below_high": row.get("pct_below_high"),
        "year_range_position": row.get("year_range_position"),
    }
    snap["expected_growth"] = resolve_spx_expected_growth(snap)
    ev = evaluate_spx_signal(snap)
    failed = [c["name"] for c in ev.get("criteria", []) if c.get("applicable") and not c.get("passed")]
    return ev["is_buy"], failed


def analyze_year(year, panels=None, high_threshold=0.70, near_low_threshold=0.03):
    panels = panels or get_panels()
    high_buys = []
    missed_lows = []
    summary = defaultdict(lambda: {"buys": 0, "high_buys": 0, "missed_lows": 0})

    for cfg in _iter_backtest_configs(panels):
        panel = cfg["panel"]
        code = cfg["code"]
        date_col = cfg["date_col"]
        buy_fn = cfg["buy_fn"]

        work = panel.copy()
        if date_col == "date_only":
            work["_dt"] = pd.to_datetime(work["date_only"])
        else:
            work["_dt"] = pd.to_datetime(work[date_col])
        sample = work[work["_dt"].dt.year == year].sort_values("_dt")
        if sample.empty:
            continue

        year_low = float(sample["close"].min())
        year_high = float(sample["close"].max())

        for _, row in sample.iterrows():
            close = float(row["close"])
            day = row["_dt"].strftime("%Y-%m-%d")
            yr_pos = row.get("year_range_position")
            if yr_pos is not None and pd.isna(yr_pos):
                yr_pos = None
            elif yr_pos is not None:
                yr_pos = float(yr_pos)
            is_buy = buy_fn(row)
            near_low = _near_year_low(close, year_low, near_low_threshold)

            if is_buy:
                summary[code]["buys"] += 1
                if yr_pos is not None and yr_pos >= high_threshold:
                    if code in CN_BROAD_CODES:
                        _, failed = _diagnose_cn_broad(row, code)
                    elif code in DIVIDEND_CODES:
                        _, failed = _diagnose_dividend(row, code)
                    elif code == "399006":
                        _, failed = _diagnose_cyb(row)
                    elif code == "HSTECH":
                        _, failed = _diagnose_hstech(row)
                    elif code == "NDX":
                        _, failed = _diagnose_ndx(row, panels.ndx_panel()[1])
                    elif code == "SPX":
                        _, failed = _diagnose_spx(row, panels.spx_panel()[1])
                    else:
                        failed = []
                    high_buys.append({
                        "year": year,
                        "code": code,
                        "name": cfg["name"],
                        "date": day,
                        "close": close,
                        "year_low": year_low,
                        "year_high": year_high,
                        "year_range_pct": yr_pos * 100,
                        "pct_above_low": row.get("pct_above_low"),
                        "pct_below_high": row.get("pct_below_high"),
                        "failed_criteria": failed,
                    })
                    summary[code]["high_buys"] += 1

            elif near_low:
                if code in CN_BROAD_CODES:
                    _, failed = _diagnose_cn_broad(row, code)
                elif code in DIVIDEND_CODES:
                    _, failed = _diagnose_dividend(row, code)
                elif code == "399006":
                    _, failed = _diagnose_cyb(row)
                elif code == "HSTECH":
                    _, failed = _diagnose_hstech(row)
                elif code == "NDX":
                    _, failed = _diagnose_ndx(row, panels.ndx_panel()[1])
                elif code == "SPX":
                    _, failed = _diagnose_spx(row, panels.spx_panel()[1])
                else:
                    failed = []
                missed_lows.append({
                    "year": year,
                    "code": code,
                    "name": cfg["name"],
                    "date": day,
                    "close": close,
                    "year_low": year_low,
                    "year_range_pct": yr_pos * 100 if yr_pos else None,
                    "pct_above_low": row.get("pct_above_low"),
                    "pct_below_high": row.get("pct_below_high"),
                    "failed_criteria": failed,
                })
                summary[code]["missed_lows"] += 1

    return high_buys, missed_lows, dict(summary)


def _top_fail_reasons(missed_lows, top_n=5):
    counts = defaultdict(int)
    for item in missed_lows:
        for reason in item.get("failed_criteria", item.get("failed", [])):
            counts[reason] += 1
    return sorted(counts.items(), key=lambda x: -x[1])[:top_n]


def print_report(years, high_threshold=0.70):
    panels = get_panels()
    all_high = []
    all_missed = []

    print(f"\n{'='*72}")
    print(f"买入质量分析（近1年区间≥{high_threshold*100:.0f}% 视为高位买入；距年低≤3% 视为漏买低点）")
    print(f"{'='*72}")

    for year in years:
        high_buys, missed_lows, summary = analyze_year(
            year, panels=panels, high_threshold=high_threshold
        )
        all_high.extend(high_buys)
        all_missed.extend(missed_lows)

        if not summary:
            continue
        print(f"\n--- {year} 年 ---")
        print(f"{'指数':<14} {'代码':<8} {'买入':>5} {'高位买':>6} {'漏低点':>6}")
        for code, s in sorted(summary.items()):
            name = next((h["name"] for h in high_buys if h["code"] == code), code)
            if not name or name == code:
                name = next((m["name"] for m in missed_lows if m["code"] == code), code)
            print(f"{name:<14} {code:<8} {s['buys']:>5} {s['high_buys']:>6} {s['missed_lows']:>6}")

        if high_buys:
            print(f"\n  高位买入样例（{year}，最多5条）:")
            for h in high_buys[:5]:
                pal = h["pct_above_low"]
                pbh = h["pct_below_high"]
                pal_t = f"{pal*100:.1f}%" if pal is not None and not pd.isna(pal) else "—"
                pbh_t = f"{pbh*100:.1f}%" if pbh is not None and not pd.isna(pbh) else "—"
                print(
                    f"    {h['date']} {h['name']} 收盘{h['close']:.2f} "
                    f"近1年{h['year_range_pct']:.0f}% 距低+{pal_t} 距高回撤{pbh_t}"
                )

        if missed_lows:
            top_fails = _top_fail_reasons(missed_lows)
            print(f"  漏买低点主因: {', '.join(f'{k}({v})' for k,v in top_fails)}")

    print(f"\n{'='*72}")
    print(f"汇总: 高位买入 {len(all_high)} 次，漏买低点 {len(all_missed)} 天")
    if all_high:
        print("\n高位买入按指数:")
        by_code = defaultdict(int)
        for h in all_high:
            by_code[f"{h['name']}({h['code']})"] += 1
        for k, v in sorted(by_code.items(), key=lambda x: -x[1]):
            print(f"  {k}: {v}")
    if all_missed:
        print("\n漏买低点按未达标项（全区间）:")
        for k, v in _top_fail_reasons(all_missed, 10):
            print(f"  {k}: {v}")


def main(argv=None):
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="分析高位买入与低位漏买")
    parser.add_argument("--year", type=int, action="append", default=[2021, 2022, 2023, 2024, 2025, 2026])
    parser.add_argument("--high-threshold", type=float, default=0.70)
    args = parser.parse_args(argv)
    print_report(args.year, high_threshold=args.high_threshold)
    return 0


if __name__ == "__main__":
    sys.exit(main())
