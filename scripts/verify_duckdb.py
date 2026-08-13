# -*- coding: utf-8 -*-
"""验证 data/market.duckdb 数据完整性。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from duckdb_cache import ensure_duckdb_cache_ready
from duckdb_market import list_a_share_codes, load_stock_kline, load_trade_calendar
from duckdb_store import get_connection, load_kv_snapshot, point_count
from market_data import configure_stdout_utf8

STOCK_CODES_SNAPSHOT = "stockdb:股票代码"


def main(argv=None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="验证 DuckDB 市场数据")
    parser.parse_args(argv)

    if not ensure_duckdb_cache_ready(verbose=True):
        print("DuckDB 不可用")
        return 1

    conn = get_connection(read_only=True)
    print(f"数据库: {conn.execute('SELECT current_database()').fetchone()}")

    total = point_count(conn)
    stock_pts = point_count(conn, domain="stock_daily")
    stock_n = conn.execute(
        "SELECT COUNT(DISTINCT entity_key) FROM ts_series WHERE domain='stock_daily'"
    ).fetchone()[0]
    cal_n = conn.execute(
        "SELECT COUNT(*) FROM ts_point WHERE series_id LIKE 'trade_calendar:%'"
    ).fetchone()[0]
    row = conn.execute(
        """
        SELECT MIN(p.trade_date), MAX(p.trade_date)
        FROM ts_point p JOIN ts_series s ON p.series_id=s.series_id
        WHERE s.domain='stock_daily' AND s.field_name='close'
        """
    ).fetchone()
    recent = conn.execute(
        """
        SELECT COUNT(DISTINCT s.entity_key) FROM ts_point p
        JOIN ts_series s ON p.series_id=s.series_id
        WHERE s.domain='stock_daily' AND s.field_name='close'
          AND p.trade_date >= DATE '2025-01-01'
        """
    ).fetchone()[0]
    kv = conn.execute("SELECT COUNT(*) FROM kv_snapshot").fetchone()[0]
    cn_idx = conn.execute(
        "SELECT COUNT(DISTINCT entity_key) FROM ts_series WHERE domain='cn_index_perf'"
    ).fetchone()[0]
    qfq_n = conn.execute(
        "SELECT COUNT(DISTINCT entity_key) FROM ts_series WHERE domain='stock_daily_qfq'"
    ).fetchone()[0]
    fhps_n = conn.execute("SELECT count(*) FROM dlv_fhps").fetchone()[0]
    risk_n = conn.execute("SELECT count(*) FROM dlv_risk_hist").fetchone()[0]
    ind_n = conn.execute("SELECT count(*) FROM dlv_industry").fetchone()[0]

    print(f"\n时点总数: {total:,}")
    print(f"stock_daily: {stock_pts:,} 点 / {stock_n} 只")
    print(f"个股 close 区间: {row[0]} ~ {row[1]}")
    print(f"2025 年有数据: {recent} 只")
    print(f"交易日历: {cal_n} 天")
    print(f"kv_snapshot: {kv} 条")
    print(f"cn_index_perf: {cn_idx} 个实体")
    print(f"stock_daily_qfq: {qfq_n} 只")
    print(f"dlv_fhps: {fhps_n:,} 行 | dlv_risk_hist: {risk_n:,} 行 | dlv_industry: {ind_n:,} 行")

    snap = load_kv_snapshot(conn, STOCK_CODES_SNAPSHOT)
    if isinstance(snap, dict):
        n = sum(len(v) for v in snap.values() if isinstance(v, list))
        print(f"股票列表快照: {n} 只")

    conn.close()

    codes = list_a_share_codes()
    print(f"\nlist_a_share_codes: {len(codes)} 只")

    cal = load_trade_calendar("2024-01-01", "2024-12-31")
    print(f"2024 交易日: {len(cal)} 天")

    sample = load_stock_kline("600519", "2024-01-01", "2024-12-31")
    print(f"样本 600519 2024 K线: {0 if sample is None else len(sample)} 行")

    ok = stock_n >= 5000 and cal_n >= 5000 and recent >= 5000
    print(f"\n结论: {'数据完整，可用' if ok else '数据可能不完整'}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
