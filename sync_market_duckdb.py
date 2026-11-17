# -*- coding: utf-8 -*-
"""统一市场数据同步编排：公网 → CSV/StockDB → DuckDB。

推荐入口（替代单独调用 sync_data_cache / sync_stockdb / import_cache）::

    python sync_market_duckdb.py              # 全量日常同步
    python sync_market_duckdb.py --force      # 强制刷新公网缓存
    python sync_market_duckdb.py --stockdb-only
    python sync_market_duckdb.py --import-only

定时任务见 setup_sync_task.bat（早盘 09:30 全量）与 setup_sync_stockdb_task.bat（收盘后 17:30 StockDB）。
"""

from __future__ import annotations

import argparse
import sys
import time

from duckdb_cache import import_csv_tree
from duckdb_store import ensure_schema, get_connection, point_count
from market_data import configure_stdout_utf8


def _phase_network(*, force: bool = False) -> None:
    print("=" * 60, flush=True)
    print("阶段 1/3：公网行情缓存（国债/中证/创业板/美股）", flush=True)
    print("=" * 60, flush=True)
    # sync_data_cache 末尾 DuckDB 段由本脚本统一处理，避免重复
    from sync_data_cache import (
        sync_bond,
        sync_cn_indicators,
        sync_cn_perf,
        sync_cyb,
        sync_us,
    )
    from index_meta import iter_tracked_index_labels

    print("\n[国债]", flush=True)
    for line in sync_bond(force=force):
        print(line, flush=True)
    print("\n[中证指标]", flush=True)
    for line in sync_cn_indicators(force=force):
        print(line, flush=True)
    print("\n[中证行情 perf]", flush=True)
    for line in sync_cn_perf(force=force):
        print(line, flush=True)
    print("\n[创业板]", flush=True)
    for line in sync_cyb(force=force):
        print(line, flush=True)
    print("\n[美股]", flush=True)
    for line in sync_us(force=force):
        print(line, flush=True)
    print("\n跟踪指数：", flush=True)
    for code, name in iter_tracked_index_labels():
        print(f"  {name} ({code})", flush=True)


def _phase_stockdb(*, force: bool = False, qfq: bool = True) -> None:
    from sync_stockdb_to_duckdb import sync_all as sync_stockdb

    print("\n" + "=" * 60, flush=True)
    print("阶段 2/3：StockDB → DuckDB（不复权全字段 + 前复权 close）", flush=True)
    print("=" * 60, flush=True)
    sync_stockdb(force=force, qfq=qfq)


def _phase_import_cache() -> None:
    print("\n" + "=" * 60, flush=True)
    print("阶段 3/3：CSV 缓存导入 DuckDB", flush=True)
    print("=" * 60, flush=True)
    print("\n[cache/cn + cyb + us]", flush=True)
    for line in import_csv_tree():
        print(line, flush=True)


def _print_summary() -> None:
    conn = get_connection(read_only=False)
    try:
        domains = conn.execute(
            """
            SELECT s.domain, COUNT(DISTINCT s.entity_key), COUNT(p.trade_date)
            FROM ts_series s
            LEFT JOIN ts_point p ON s.series_id = p.series_id
            GROUP BY s.domain
            ORDER BY 3 DESC
            """
        ).fetchall()
        print("\n" + "=" * 60, flush=True)
        print("DuckDB 汇总", flush=True)
        print("=" * 60, flush=True)
        print(f"  时点总数: {point_count(conn):,}", flush=True)
        for dom, ent, pts in domains[:12]:
            print(f"  {dom}: {ent} 实体 / {pts:,} 点", flush=True)
        print(f"  数据库: {ensure_schema()}", flush=True)
    finally:
        conn.close()


def sync_market_duckdb(
    *,
    force: bool = False,
    network: bool = True,
    stockdb: bool = True,
    import_cache: bool = True,
    qfq: bool = True,
) -> None:
    t0 = time.time()
    ensure_schema()
    if network:
        _phase_network(force=force)
    if stockdb:
        _phase_stockdb(force=force, qfq=qfq)
    if import_cache:
        _phase_import_cache()
    _safe_print_summary()
    print(f"\n总耗时: {time.time() - t0:.1f}s", flush=True)


def _safe_print_summary() -> None:
    try:
        _print_summary()
    except Exception as exc:
        print(f"\n汇总统计跳过: {exc}", flush=True)


def main(argv=None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="统一同步全部市场数据到 DuckDB")
    parser.add_argument("--force", action="store_true", help="强制刷新公网侧缓存")
    parser.add_argument("--network-only", action="store_true", help="仅公网行情（阶段1）")
    parser.add_argument("--stockdb-only", action="store_true", help="仅 StockDB（阶段3）")
    parser.add_argument("--import-only", action="store_true", help="仅 CSV 导入（阶段4）")
    parser.add_argument("--no-qfq", action="store_true", help="StockDB 阶段跳过前复权 close")
    parser.add_argument("--skip-network", action="store_true", help="跳过公网拉取")
    parser.add_argument("--skip-stockdb", action="store_true", help="跳过 StockDB")
    args = parser.parse_args(argv)

    only_flags = sum(
        1
        for f in (
            args.network_only,
            args.stockdb_only,
            args.import_only,
        )
        if f
    )
    if only_flags > 1:
        print("--*-only 选项互斥", flush=True)
        return 2

    try:
        if args.network_only:
            _phase_network(force=args.force)
        elif args.stockdb_only:
            _phase_stockdb(force=args.force, qfq=not args.no_qfq)
        elif args.import_only:
            _phase_import_cache()
        else:
            sync_market_duckdb(
                force=args.force,
                network=not args.skip_network,
                stockdb=not args.skip_stockdb,
                import_cache=True,
                qfq=not args.no_qfq,
            )
        if args.network_only or not any((args.stockdb_only, args.import_only)):
            _print_summary()
    except Exception as exc:
        print(f"\n同步失败: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
