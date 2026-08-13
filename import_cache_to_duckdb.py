"""将现有 cache/ CSV/JSON 一次性导入 data/market.duckdb。"""

from __future__ import annotations

import argparse
import sys

from duckdb_cache import import_csv_tree
from duckdb_dlv_cache import import_dividend_lowvol_tree
from duckdb_store import ensure_schema, get_connection, point_count
from market_data import configure_stdout_utf8


def main(argv=None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="导入 cache/ 到 DuckDB")
    parser.add_argument(
        "--include-qfq-csv",
        action="store_true",
        help="同时导入 cache/dividend_lowvol/kline_*.csv（慢，一般用 stockdb qfq 同步）",
    )
    args = parser.parse_args(argv)

    path = ensure_schema()
    print(f"导入本地 CSV/JSON 缓存 → {path}")
    lines = import_csv_tree()
    if not lines:
        print("  未找到可导入文件 (cache/cn|cyb|us)")
    else:
        for line in lines:
            print(line)

    print("\n导入 dividend_lowvol 策略缓存…")
    dlv_lines = import_dividend_lowvol_tree(include_qfq_csv=args.include_qfq_csv)
    if not dlv_lines:
        print("  未找到 cache/dividend_lowvol 文件")
    else:
        for line in dlv_lines:
            print(line)

    conn = get_connection()
    print(f"\n时点总数: {point_count(conn)}")
    print(f"stock_daily: {point_count(conn, domain='stock_daily')} 点")
    return 0


if __name__ == "__main__":
    sys.exit(main())
