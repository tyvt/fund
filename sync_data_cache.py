"""预拉取并增量更新全部指数本地缓存（自基日起）。"""

from __future__ import annotations

import argparse
import os
import sys

from config import US_INDEX_KEYS
from data_cache import offline_mode_enabled
from cyb_data import (
    build_cyb_valuation_panel,
    fetch_cyb_dividend_history,
    fetch_cyb_pb_history,
    fetch_cyb_pe_history,
    fetch_cyb_price_history,
)
from index_meta import iter_all_csindex_perf_codes, iter_tracked_index_labels
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


_FAILED = object()


def _sync_step(name: str, fetch, failures: list[str]):
    attempts = 1 if offline_mode_enabled() else 2
    for attempt in range(attempts):
        try:
            value = fetch()
            empty = value is None
            if hasattr(value, "empty"):
                empty = value.empty
            elif isinstance(value, (dict, list, str)):
                empty = not value
            elif isinstance(value, tuple):
                empty = not value or all(item is None for item in value)
            if empty and attempt + 1 < attempts:
                print(f"  {name}: 返回空数据，重试一次")
                continue
            return value
        except Exception as exc:
            if attempt + 1 < attempts:
                print(f"  {name}: 拉取失败，重试一次 ({exc})")
                continue
            failures.append(f"{name}: {exc}")
            print(f"  {name}: 失败 ({exc})")
    return _FAILED


def _record_empty(name: str, value, failures: list[str]) -> None:
    if value is _FAILED:
        return
    empty = value is None
    if hasattr(value, "empty"):
        empty = value.empty
    elif isinstance(value, (dict, list, str, tuple)):
        empty = not value
    if empty:
        failures.append(f"{name}: 无数据")
        print(f"  {name}: 无数据")


def sync_cn_perf(force: bool = False, failures: list[str] | None = None) -> list[str]:
    failures = failures if failures is not None else []
    lines = []
    for code in iter_all_csindex_perf_codes():
        label = f"中证行情 {code}"
        hist = _sync_step(
            label, lambda code=code: load_index_perf_history(code, force=force), failures
        )
        _record_empty(label, hist, failures)
        n = len(hist) if hist is not _FAILED and hist is not None else 0
        lines.append(f"  {code}: {n} 行")
    return lines


def sync_cn_indicators(
    force: bool = False, failures: list[str] | None = None
) -> list[str]:
    from config import DIVIDEND_TOTAL_RETURN_INDEX

    failures = failures if failures is not None else []
    skip = frozenset(DIVIDEND_TOTAL_RETURN_INDEX.values())
    lines = []
    for code in iter_all_csindex_perf_codes():
        if code in skip:
            lines.append(f"  {code}: 跳过（全收益指数无指标文件）")
            continue
        label = f"中证指标 {code}"
        history = _sync_step(label, lambda code=code: read_indicator_history(code), failures)
        _record_empty(label, history, failures)
        lines.append(f"  {code}: 指标已同步" if history is not _FAILED and history is not None else f"  {code}: 指标缺失")
    return lines


def sync_bond(
    force: bool = False, failures: list[str] | None = None
) -> list[str]:
    failures = failures if failures is not None else []
    hist = _sync_step("国债历史", get_gov_bond_yield_history, failures)
    _record_empty("国债历史", hist, failures)
    latest = _sync_step("国债最新值", get_gov_bond_yield, failures)
    if latest is not _FAILED and (not latest or len(latest) < 2 or latest[1] is None):
        failures.append("国债最新值: 无数据")
        print("  国债最新值: 无数据")
    n = len(hist) if hist is not _FAILED and hist is not None else 0
    latest_txt = f"{latest[1]}" if latest is not _FAILED and latest and latest[1] else "—"
    return [f"  国债历史: {n} 行；最新 {latest_txt}"]


def sync_cyb(force: bool = False, failures: list[str] | None = None) -> list[str]:
    failures = failures if failures is not None else []
    for label, fetch in (
        ("创业板 PE", fetch_cyb_pe_history),
        ("创业板 PB", fetch_cyb_pb_history),
        ("创业板股息率", fetch_cyb_dividend_history),
        ("创业板价格", fetch_cyb_price_history),
    ):
        value = _sync_step(label, fetch, failures)
        _record_empty(label, value, failures)
    panel = _sync_step("创业板估值面板", build_cyb_valuation_panel, failures)
    _record_empty("创业板估值面板", panel, failures)
    n = len(panel) if panel is not _FAILED and panel is not None else 0
    return [f"  创业板面板: {n} 行"]


def sync_us(force: bool = False, failures: list[str] | None = None) -> list[str]:
    failures = failures if failures is not None else []
    _sync_step("美股缓存整理", trim_us_index_cache, failures)
    lines = []
    for key in US_INDEX_KEYS:
        upper = key.upper()
        pe = _sync_step(f"{upper} PE", lambda key=key: fetch_pe_payload(key), failures)
        _record_empty(f"{upper} PE", pe, failures)
        prices = _sync_step(f"{upper} 价格", lambda key=key: fetch_price_history(key), failures)
        _record_empty(f"{upper} 价格", prices, failures)
        bond = _sync_step(
            f"{upper} 10Y", lambda key=key: fetch_us10y_history(key=key), failures
        )
        _record_empty(f"{upper} 10Y", bond, failures)
        dividends = _sync_step(
            f"{upper} 股息率", lambda key=key: fetch_dividend_yield_proxy(key), failures
        )
        _record_empty(f"{upper} 股息率", dividends, failures)
        panel = _sync_step(
            f"{upper} 估值面板", lambda key=key: build_daily_valuation_panel(key), failures
        )
        daily = panel[0] if panel is not _FAILED and panel else None
        _record_empty(f"{upper} 日频面板", daily, failures)
        pn = len(prices) if prices is not _FAILED and prices is not None else 0
        dn = len(daily) if daily is not None else 0
        lines.append(f"  {upper}: 价格 {pn} 行，日频面板 {dn} 行")
    return lines


def sync_all(force: bool = False, check_local: bool = False) -> int:
    if check_local:
        os.environ["FUND_OFFLINE"] = "1"
        print("检查本地离线缓存（不会访问网络）...")
    else:
        print("同步本地数据缓存（自各指数基日起，增量合并）...")

    failures: list[str] = []
    groups = (
        ("国债", sync_bond),
        ("中证指标", sync_cn_indicators),
        ("中证行情 perf", sync_cn_perf),
        ("创业板", sync_cyb),
        ("美股", sync_us),
    )
    for title, sync_fn in groups:
        print(f"\n[{title}]")
        try:
            lines = sync_fn(force=force, failures=failures)
            for line in lines:
                print(line)
        except Exception as exc:
            failures.append(f"{title}: {exc}")
            print(f"  {title}同步失败: {exc}")

    print("\n跟踪指数：")
    for code, name in iter_tracked_index_labels():
        print(f"  {name} ({code})")

    if not check_local:
        print("\n[买入额度]")
        try:
            from config import BUY_AMOUNT_POSITION_ALLOC_ENABLED

            if BUY_AMOUNT_POSITION_ALLOC_ENABLED:
                from buy_amount_allocation import (
                    format_allocation_note,
                    get_position_allocation,
                )

                alloc = get_position_allocation(force=True)
                print(f"  {format_allocation_note(alloc)}")
            else:
                from buy_amount_ranking import compute_index_ranking, format_ranking_note

                alloc = compute_index_ranking(force=True)
                print(f"  {format_ranking_note(alloc)}")
        except Exception as exc:
            print(f"  额度分配刷新失败: {exc}")

    print("\n[DuckDB]")
    print("  请运行统一同步: python sync_market_duckdb.py")
    print("  （或收盘后 StockDB: python sync_stockdb_to_duckdb.py）")
    print("\n缓存目录: cache/（当日已同步则跳过重拉，次日自动增量更新）")
    print("DuckDB: data/market.duckdb（统一入口 sync_market_duckdb.py）")

    if failures:
        print(f"\n缓存不完整，{len(failures)} 项需要处理：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\n指数数据缓存完整。")
    return 0


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
    parser.add_argument(
        "--check-local",
        action="store_true",
        help="只检查已有指数缓存，不访问网络；缺失项以非零状态返回",
    )
    args = parser.parse_args(argv)
    try:
        return sync_all(force=args.force, check_local=args.check_local)
    except Exception as exc:
        print(f"同步失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
