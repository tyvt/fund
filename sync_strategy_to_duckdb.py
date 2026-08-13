# -*- coding: utf-8 -*-
"""策略侧数据源同步：公网拉取 + dividend_lowvol 缓存写入 DuckDB。"""

from __future__ import annotations

import argparse
import sys

from duckdb_dlv_cache import import_dividend_lowvol_tree
from duckdb_store import ensure_schema, get_connection, point_count, upsert_dlv_industry
from index_meta import iter_extra_csindex_perf_codes
from market_data import configure_stdout_utf8


def sync_fhps(*, force: bool = False, backtest_start: str = "2015-01-01") -> list[str]:
    from dividend_lowvol_rotation.dividend import load_fhps_all_records

    df = load_fhps_all_records(refresh=force, backtest_start=backtest_start)
    n = len(df) if df is not None else 0
    return [f"  分红 fhps 缓存: {n} 条"]


def sync_industry(*, force: bool = False) -> list[str]:
    from dividend_lowvol_rotation.industry import load_industry_table

    mapping, label = load_industry_table(refresh=force)
    ensure_schema()
    conn = get_connection()
    try:
        if mapping:
            df = __import__("pandas").DataFrame(
                [{"code": k, "industry": v, "source": label} for k, v in mapping.items()]
            )
            n = upsert_dlv_industry(conn, df)
            return [f"  行业 ({label}): {n} 只 → dlv_industry"]
        return ["  行业: 无数据"]
    finally:
        conn.close()


def sync_extra_indices(*, force: bool = False) -> list[str]:
    from market_data import load_index_perf_history

    lines: list[str] = []
    for code in iter_extra_csindex_perf_codes():
        hist = load_index_perf_history(code, force=force)
        n = len(hist) if hist is not None else 0
        lines.append(f"  指数 perf {code}: {n} 行（→ cn_index_perf）")
    return lines


def sync_market_pe(*, force: bool = False) -> list[str]:
    from dividend_lowvol_rotation.config import MARKET_VALUATION_INDEX
    from dividend_lowvol_rotation.market_valuation import load_market_pe_history

    hist = load_market_pe_history(refresh=force)
    n = len(hist) if hist is not None and not hist.empty else 0
    return [f"  全市场 PE ({MARKET_VALUATION_INDEX}): {n} 日"]


def import_dlv_cache() -> list[str]:
    lines = import_dividend_lowvol_tree()
    return lines or ["  dividend_lowvol 缓存: 无可导入文件"]


def sync_strategy(
    *,
    force: bool = False,
    skip_network: bool = False,
    skip_import: bool = False,
) -> None:
    print("策略数据 → DuckDB…", flush=True)
    if not skip_network:
        print("\n[分红 fhps]", flush=True)
        for line in sync_fhps(force=force):
            print(line, flush=True)
        print("\n[行业分类]", flush=True)
        for line in sync_industry(force=force):
            print(line, flush=True)
        print("\n[策略基准指数 perf]", flush=True)
        for line in sync_extra_indices(force=force):
            print(line, flush=True)
        print("\n[全市场估值 PE]", flush=True)
        for line in sync_market_pe(force=force):
            print(line, flush=True)
    if not skip_import:
        print("\n[导入 dividend_lowvol 缓存]", flush=True)
        for line in import_dlv_cache():
            print(line, flush=True)
    conn = get_connection()
    try:
        fhps_n = conn.execute("SELECT count(*) FROM dlv_fhps").fetchone()[0]
        risk_n = conn.execute("SELECT count(*) FROM dlv_risk_hist").fetchone()[0]
        ind_n = conn.execute("SELECT count(*) FROM dlv_industry").fetchone()[0]
        qfq_n = conn.execute(
            "SELECT COUNT(DISTINCT entity_key) FROM ts_series WHERE domain='stock_daily_qfq'"
        ).fetchone()[0]
        print(
            f"\n策略表汇总: fhps={fhps_n:,} risk={risk_n:,} industry={ind_n:,} "
            f"qfq={qfq_n} 只 | stock_daily_qfq 时点 {point_count(conn, domain='stock_daily_qfq'):,}",
            flush=True,
        )
    finally:
        conn.close()


def main(argv=None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="策略侧数据同步到 DuckDB")
    parser.add_argument("--force", action="store_true", help="强制刷新公网缓存")
    parser.add_argument("--import-only", action="store_true", help="仅导入本地 CSV，不访问公网")
    parser.add_argument("--network-only", action="store_true", help="仅拉公网，不导入 CSV")
    args = parser.parse_args(argv)
    try:
        sync_strategy(
            force=args.force,
            skip_network=args.import_only,
            skip_import=args.network_only,
        )
    except Exception as exc:
        print(f"同步失败: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
