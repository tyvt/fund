"""预拉取并增量更新全部指数本地缓存（自基日起）。"""

from __future__ import annotations

import argparse
import sys

from config import US_INDEX_KEYS
from cyb_data import (
    build_cyb_valuation_panel,
    fetch_cyb_dividend_history,
    fetch_cyb_pb_history,
    fetch_cyb_pe_history,
    fetch_cyb_price_history,
)
from hstech_data import fetch_hstech_pe_dividend_history, fetch_hstech_price_history
from index_meta import iter_tracked_csindex_perf_codes, iter_tracked_index_labels
from market_data import (
    configure_stdout_utf8,
    get_gov_bond_yield,
    get_gov_bond_yield_history,
    load_index_perf_history,
    read_indicator_history,
)
from us_index_data import (
    build_daily_valuation_panel,
    fetch_dividend_yield_proxy,
    fetch_pe_payload,
    fetch_price_history,
    fetch_us10y_history,
    trim_us_index_cache,
)


def sync_cn_perf(force: bool = False) -> list[str]:
    lines = []
    for code in iter_tracked_csindex_perf_codes():
        hist = load_index_perf_history(code, force=force)
        n = len(hist) if hist is not None else 0
        lines.append(f"  {code}: {n} 行")
    return lines


def sync_cn_indicators() -> list[str]:
    from config import DIVIDEND_TOTAL_RETURN_INDEX

    skip = frozenset(DIVIDEND_TOTAL_RETURN_INDEX.values())
    lines = []
    for code in iter_tracked_csindex_perf_codes():
        if code in skip:
            lines.append(f"  {code}: 跳过（全收益指数无指标文件）")
            continue
        read_indicator_history(code)
        lines.append(f"  {code}: 指标已同步")
    return lines


def sync_bond(force: bool = False) -> list[str]:
    hist = get_gov_bond_yield_history()
    latest = get_gov_bond_yield()
    n = len(hist) if hist is not None else 0
    latest_txt = f"{latest[1]}" if latest[1] else "—"
    return [f"  国债历史: {n} 行；最新 {latest_txt}"]


def sync_cyb(force: bool = False) -> list[str]:
    fetch_cyb_pe_history()
    fetch_cyb_pb_history()
    fetch_cyb_dividend_history()
    fetch_cyb_price_history()
    panel = build_cyb_valuation_panel()
    n = len(panel) if panel is not None else 0
    return [f"  创业板面板: {n} 行"]


def sync_hstech(force: bool = False) -> list[str]:
    fetch_hstech_pe_dividend_history()
    fetch_hstech_price_history()
    return ["  恒生科技: PE/价格已同步"]


def sync_us(force: bool = False) -> list[str]:
    lines = list(trim_us_index_cache())
    for key in US_INDEX_KEYS:
        fetch_pe_payload(key)
        prices = fetch_price_history(key)
        fetch_us10y_history(key=key)
        fetch_dividend_yield_proxy(key)
        daily, _ = build_daily_valuation_panel(key)
        pn = len(prices) if prices is not None else 0
        dn = len(daily) if daily is not None else 0
        lines.append(f"  {key.upper()}: 价格 {pn} 行，日频面板 {dn} 行")
    return lines


def sync_all(force: bool = False) -> None:
    print("同步本地数据缓存（自各指数基日起，增量合并）...")
    print("\n[国债]")
    for line in sync_bond(force=force):
        print(line)

    print("\n[中证指标]")
    for line in sync_cn_indicators(force=force):
        print(line)

    print("\n[中证行情 perf]")
    for line in sync_cn_perf(force=force):
        print(line)

    print("\n[创业板]")
    for line in sync_cyb(force=force):
        print(line)

    print("\n[恒生科技]")
    for line in sync_hstech(force=force):
        print(line)

    print("\n[美股]")
    for line in sync_us(force=force):
        print(line)

    print("\n跟踪指数：")
    for code, name in iter_tracked_index_labels():
        print(f"  {name} ({code})")
    print("\n缓存目录: cache/（当日已同步则跳过重拉，次日自动增量更新）")


def main(argv=None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(
        description="预拉取/增量更新全部指数本地数据缓存"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略当日缓存新鲜度，强制重新拉取并合并",
    )
    args = parser.parse_args(argv)
    try:
        sync_all(force=args.force)
    except Exception as exc:
        print(f"同步失败: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
