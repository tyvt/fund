"""针对指定指数分析 2021 至今低位漏买 / 高位买入及根因。"""

import sys
from collections import defaultdict

import pandas as pd

from analyze_buy_quality import (
    _diagnose_cn_broad,
    _diagnose_hstech,
    _diagnose_us_index,
    _near_year_low,
    _top_fail_reasons,
)
from backtest_buy_signals import get_panels, _iter_backtest_configs
from market_data import configure_stdout_utf8

TARGET_CODES = {"000300", "000852", "000688", "HSTECH", "NDX", "SPX"}


def analyze_indices(years, high_threshold=0.65, near_low_threshold=0.03):
    panels = get_panels()
    high_buys = []
    missed_lows = []
    buys_by_year = defaultdict(lambda: defaultdict(int))

    for year in years:
        for cfg in _iter_backtest_configs(panels):
            code = cfg["code"]
            if code not in TARGET_CODES:
                continue
            panel = cfg["panel"]
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
                if yr_pos is not None and not pd.isna(yr_pos):
                    yr_pos = float(yr_pos)
                else:
                    yr_pos = None
                is_buy = buy_fn(row)
                near_low = _near_year_low(close, year_low, near_low_threshold)

                if is_buy:
                    buys_by_year[year][code] += 1
                    if yr_pos is not None and yr_pos >= high_threshold:
                        failed = _diag(row, code, panels)
                        high_buys.append(
                            {
                                "year": year,
                                "code": code,
                                "name": cfg["name"],
                                "date": day,
                                "close": close,
                                "year_low": year_low,
                                "year_high": year_high,
                                "range_pct": yr_pos * 100,
                                "pct_above_low": row.get("pct_above_low"),
                                "pct_below_high": row.get("pct_below_high"),
                                "failed": failed,
                            }
                        )
                elif near_low:
                    failed = _diag(row, code, panels)
                    missed_lows.append(
                        {
                            "year": year,
                            "code": code,
                            "name": cfg["name"],
                            "date": day,
                            "close": close,
                            "range_pct": yr_pos * 100 if yr_pos is not None else None,
                            "pct_above_low": row.get("pct_above_low"),
                            "pct_below_high": row.get("pct_below_high"),
                            "failed": failed,
                        }
                    )

    return high_buys, missed_lows, buys_by_year


def _diag(row, code, panels):
    if code in {"000300", "000852", "000688"}:
        _, failed = _diagnose_cn_broad(row, code)
    elif code == "HSTECH":
        _, failed = _diagnose_hstech(row)
    elif code == "NDX":
        _, failed = _diagnose_us_index("ndx", row, panels.us_index_panel("ndx")[1])
    elif code == "SPX":
        _, failed = _diagnose_us_index("spx", row, panels.us_index_panel("spx")[1])
    else:
        failed = []
    return failed


def main():
    configure_stdout_utf8()
    years = [2021, 2022, 2023, 2024, 2025, 2026]
    high_buys, missed_lows, buys = analyze_indices(years)

    print("=" * 72)
    print("目标指数：沪深300 / 中证1000 / 科创50 / 恒生科技 / 纳指100 / 标普500")
    print("=" * 72)

    print("\n## 各年买入次数")
    for year in years:
        if year not in buys:
            continue
        print(f"\n{year}:")
        for code in sorted(TARGET_CODES):
            cnt = buys[year].get(code, 0)
            if cnt:
                print(f"  {code}: {cnt}")

    print(f"\n## 高位买入（近1年区间≥65%）: {len(high_buys)} 次")
    by_code = defaultdict(int)
    for h in high_buys:
        by_code[h["code"]] += 1
    for code, n in sorted(by_code.items(), key=lambda x: -x[1]):
        print(f"  {code}: {n}")
    for h in high_buys[:12]:
        pal = h["pct_above_low"]
        pbh = h["pct_below_high"]
        pal_t = f"{pal*100:.1f}%" if pal is not None and not pd.isna(pal) else "—"
        pbh_t = f"{pbh*100:.1f}%" if pbh is not None and not pd.isna(pbh) else "—"
        print(
            f"  {h['date']} {h['name']} 收盘{h['close']:.2f} "
            f"近1年区间{h['range_pct']:.0f}% 距低+{pal_t} 回撤{pbh_t} "
            f"未过: {','.join(h['failed']) or '—'}"
        )

    print(f"\n## 漏买低点（距年低≤3%）: {len(missed_lows)} 天")
    by_code_m = defaultdict(int)
    for m in missed_lows:
        by_code_m[m["code"]] += 1
    for code, n in sorted(by_code_m.items(), key=lambda x: -x[1]):
        print(f"  {code}: {n}")
    print("\n漏买主因:")
    for k, v in _top_fail_reasons(missed_lows, 12):
        print(f"  {k}: {v}")

    # 按指数分主因
    print("\n## 分指数漏买主因（前3）")
    for code in sorted(TARGET_CODES):
        items = [m for m in missed_lows if m["code"] == code]
        if not items:
            continue
        name = items[0]["name"]
        tops = _top_fail_reasons(items, 3)
        print(f"  {name}({code}): {', '.join(f'{k}({v})' for k,v in tops)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
