"""从本地 StockDB 同步交易日历与个股日 K 到 DuckDB（支持增量）。"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta

import pandas as pd

from config import STOCKDB_HOST, STOCKDB_PORT, STOCKDB_SDK_PATH
from duckdb_store import (
    STOCK_DAILY_FIELDS,
    STOCK_QFQ_DOMAIN,
    STOCK_QFQ_FIELDS,
    bulk_register_series_fields,
    bulk_update_sync_meta_from_points,
    ensure_schema,
    get_connection,
    is_synced_today,
    load_kv_snapshot,
    max_trade_dates_batch,
    point_count,
    register_series_fields,
    series_id,
    update_sync_meta,
    upsert_kv_snapshot,
    upsert_points_long,
)
from market_data import configure_stdout_utf8

CALENDAR_CODE = "000001"
CALENDAR_DOMAIN = "trade_calendar"
STOCK_DOMAIN = "stock_daily"

# StockDB「股票代码」分类：0/3/6/9 为 A 股股票，1/5 主要为 ETF/基金/REIT
A_SHARE_CATEGORIES = ("0", "3", "6", "9")

REGISTER_CHUNK = 500
FETCH_RETRIES = 3
FETCH_RETRY_DELAY = 2.0


def _get_stockdb_client():
    if str(STOCKDB_SDK_PATH) not in sys.path:
        sys.path.insert(0, str(STOCKDB_SDK_PATH))
    from stock_sdk import StockDBClient

    return StockDBClient(host=STOCKDB_HOST, port=STOCKDB_PORT)


STOCK_CODES_SNAPSHOT = "stockdb:股票代码"


def _codes_from_payload(payload: dict, scope: str) -> list[str]:
    codes: list[str] = []
    keys = payload.keys() if scope == "all" else A_SHARE_CATEGORIES
    for key in keys:
        items = payload.get(key)
        if isinstance(items, list):
            codes.extend(str(c) for c in items)
    return sorted(set(codes))


def _list_stock_codes(scope: str = "all", conn=None) -> list[str]:
    last_exc: Exception | None = None
    for attempt in range(FETCH_RETRIES):
        try:
            if str(STOCKDB_SDK_PATH) not in sys.path:
                sys.path.insert(0, str(STOCKDB_SDK_PATH))
            from stockdb import init

            rd = init(host=STOCKDB_HOST, port=STOCKDB_PORT)
            payload = rd.get("股票代码")
            if not payload:
                return []
            if not isinstance(payload, dict):
                try:
                    payload = dict(payload)
                except (TypeError, ValueError):
                    return []
            if conn is not None:
                upsert_kv_snapshot(conn, STOCK_CODES_SNAPSHOT, payload)
            return _codes_from_payload(payload, scope)
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < FETCH_RETRIES:
                time.sleep(FETCH_RETRY_DELAY * (attempt + 1))
    raise last_exc or RuntimeError("StockDB 股票列表拉取失败")


def _list_codes_from_duckdb(conn, scope: str = "all") -> list[str]:
    """从已注册 series 回退读取代码列表（StockDB 不可用时）。"""
    rows = conn.execute(
        "SELECT DISTINCT entity_key FROM ts_series WHERE domain = ?",
        [STOCK_DOMAIN],
    ).fetchall()
    codes = sorted({str(r[0]) for r in rows if r and r[0]})
    return codes


def _resolve_stock_codes(conn, scope: str = "all") -> tuple[list[str], str]:
    try:
        return _list_stock_codes(scope, conn=conn), "stockdb"
    except Exception as exc:
        payload = load_kv_snapshot(conn, STOCK_CODES_SNAPSHOT)
        if isinstance(payload, dict):
            codes = _codes_from_payload(payload, scope)
            if codes:
                print(
                    f"  StockDB 列表失败，使用本地快照 {len(codes)} 只: {exc}",
                    flush=True,
                )
                return codes, "snapshot"
        cached = _list_codes_from_duckdb(conn, scope)
        if cached:
            print(f"  StockDB 列表失败，回退 DuckDB 已注册 {len(cached)} 只: {exc}", flush=True)
            return cached, "duckdb"
        raise


def _int_date_to_iso(value) -> str | None:
    if value is None:
        return None
    text = str(int(value))
    if len(text) != 8:
        return None
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def _kline_dict_to_points(kline_dict: dict[str, list]) -> pd.DataFrame:
    """多只股票 K 线 → 长表，先合并再 melt 减少重复开销。"""
    frames: list[pd.DataFrame] = []
    for code, records in kline_dict.items():
        if not records:
            continue
        df = pd.DataFrame(records)
        if df.empty or "date" not in df.columns:
            continue
        df["_code"] = code
        frames.append(df)

    if not frames:
        return pd.DataFrame(columns=["series_id", "trade_date", "value"])

    merged = pd.concat(frames, ignore_index=True)
    merged["trade_date"] = merged["date"].map(_int_date_to_iso)
    merged = merged.dropna(subset=["trade_date"])
    if merged.empty:
        return pd.DataFrame(columns=["series_id", "trade_date", "value"])

    cols = [c for c in STOCK_DAILY_FIELDS if c in merged.columns]
    if not cols:
        return pd.DataFrame(columns=["series_id", "trade_date", "value"])

    if "is_st" in cols:
        merged["is_st"] = merged["is_st"].map(lambda x: 1.0 if x else 0.0)
    num_cols = [c for c in cols if c != "is_st"]
    if num_cols:
        merged[num_cols] = merged[num_cols].apply(pd.to_numeric, errors="coerce")

    long = merged.melt(
        id_vars=["_code", "trade_date"],
        value_vars=cols,
        var_name="field_name",
        value_name="value",
    )
    long = long.dropna(subset=["value"])
    if long.empty:
        return pd.DataFrame(columns=["series_id", "trade_date", "value"])

    long["series_id"] = (
        STOCK_DOMAIN + ":" + long["_code"].astype(str) + ":" + long["field_name"]
    )
    return long[["series_id", "trade_date", "value"]]


def _qfq_records_to_points(kline_dict: dict[str, list]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for code, records in kline_dict.items():
        if not records:
            continue
        df = pd.DataFrame(records)
        if df.empty or "date" not in df.columns or "close" not in df.columns:
            continue
        df["_code"] = str(code)
        frames.append(df[["date", "close", "_code"]])
    if not frames:
        return pd.DataFrame(columns=["series_id", "trade_date", "value"])
    merged = pd.concat(frames, ignore_index=True)
    merged["trade_date"] = merged["date"].map(_int_date_to_iso)
    merged["close"] = pd.to_numeric(merged["close"], errors="coerce")
    merged = merged.dropna(subset=["trade_date", "close"])
    if merged.empty:
        return pd.DataFrame(columns=["series_id", "trade_date", "value"])
    merged["series_id"] = STOCK_QFQ_DOMAIN + ":" + merged["_code"].astype(str) + ":close"
    merged["value"] = merged["close"]
    return merged[["series_id", "trade_date", "value"]]


def _qfq_df_to_points(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "code" not in df.columns:
        return pd.DataFrame(columns=["series_id", "trade_date", "value"])
    out = df.copy()
    out["trade_date"] = out["date"].map(_int_date_to_iso)
    out["value"] = pd.to_numeric(out["close"], errors="coerce")
    out = out.dropna(subset=["trade_date", "value"])
    if out.empty:
        return pd.DataFrame(columns=["series_id", "trade_date", "value"])
    out["series_id"] = STOCK_QFQ_DOMAIN + ":" + out["code"].astype(str) + ":close"
    return out[["series_id", "trade_date", "value"]]


def _fetch_kline(client, codes: list[str], start: str | None, end: str, *, fq=None) -> dict[str, list]:
    kwargs: dict = {"frequency": "1d", "fq": fq}
    if start:
        kwargs["start"] = start
        kwargs["end"] = end
    last_exc: Exception | None = None
    for attempt in range(FETCH_RETRIES):
        try:
            result = client.get_data(codes, **kwargs)
            if isinstance(result, dict):
                return result
            return {}
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < FETCH_RETRIES:
                time.sleep(FETCH_RETRY_DELAY * (attempt + 1))
    raise last_exc or RuntimeError("StockDB 拉取失败")


def _fetch_kline_split(
    client, codes: list[str], start: str | None, end: str, *, fq=None
) -> dict[str, list]:
    """拉取 K 线；失败时自动拆半重试，避免整批超时作废。"""
    if not codes:
        return {}
    try:
        return _fetch_kline(client, codes, start, end, fq=fq)
    except Exception:
        if len(codes) <= 1:
            return {}
        mid = len(codes) // 2
        left = _fetch_kline_split(client, codes[:mid], start, end, fq=fq)
        right = _fetch_kline_split(client, codes[mid:], start, end, fq=fq)
        left.update(right)
        return left


def _fetch_kline_qfq_df(
    client, codes: list[str], start: str | None, end: str
) -> pd.DataFrame:
    kwargs: dict = {
        "frequency": "1d",
        "fq": "qfq",
        "fields": "date,code,close",
        "as_df": True,
    }
    if start:
        kwargs["start"] = start
        kwargs["end"] = end
    last_exc: Exception | None = None
    for attempt in range(FETCH_RETRIES):
        try:
            result = client.get_data(codes, **kwargs)
            if isinstance(result, pd.DataFrame):
                return _qfq_df_to_points(result)
            if isinstance(result, dict):
                return _qfq_records_to_points(result)
            return pd.DataFrame()
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < FETCH_RETRIES:
                time.sleep(FETCH_RETRY_DELAY * (attempt + 1))
    raise last_exc or RuntimeError("StockDB qfq 拉取失败")


def _fetch_kline_qfq_split(
    client, codes: list[str], start: str | None, end: str
) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    try:
        return _fetch_kline_qfq_df(client, codes, start, end)
    except Exception:
        if len(codes) <= 1:
            return pd.DataFrame()
        mid = len(codes) // 2
        left = _fetch_kline_qfq_split(client, codes[:mid], start, end)
        right = _fetch_kline_qfq_split(client, codes[mid:], start, end)
        if left is None or left.empty:
            return right
        if right is None or right.empty:
            return left
        return pd.concat([left, right], ignore_index=True)


def _ensure_qfq_series_registered(conn, codes: list[str]) -> None:
    bulk_register_series_fields(
        conn,
        codes,
        domain=STOCK_QFQ_DOMAIN,
        fields=STOCK_QFQ_FIELDS,
        source="stockdb_qfq",
    )


def _ensure_all_series_registered(conn, codes: list[str]) -> None:
    total = len(codes)
    for i in range(0, total, REGISTER_CHUNK):
        chunk = codes[i : i + REGISTER_CHUNK]
        bulk_register_series_fields(
            conn,
            chunk,
            domain=STOCK_DOMAIN,
            fields=STOCK_DAILY_FIELDS,
            source="stockdb",
        )
        done = min(i + REGISTER_CHUNK, total)
        if done == total or done % 2000 == 0 or i == 0:
            print(f"  预注册 series: {done}/{total}", flush=True)


def sync_trade_calendar(*, force: bool = False) -> str:
    ensure_schema()
    conn = get_connection()
    dataset = f"{CALENDAR_DOMAIN}:{CALENDAR_CODE}"
    if not force and is_synced_today(conn, dataset):
        row = conn.execute(
            "SELECT count(*) FROM ts_point WHERE series_id = ?",
            [series_id(CALENDAR_DOMAIN, CALENDAR_CODE, "session")],
        ).fetchone()
        n = int(row[0]) if row else 0
        return f"  交易日历: 今日已同步 ({n} 天)"

    client = _get_stockdb_client()
    df = client.get_data(
        CALENDAR_CODE,
        frequency="1d",
        fields="date",
        fq=None,
        as_df=True,
    )
    if df is None or df.empty:
        raise RuntimeError("stockdb 未返回交易日历数据")

    register_series_fields(
        conn,
        domain=CALENDAR_DOMAIN,
        entity_key=CALENDAR_CODE,
        fields=["session"],
        source="stockdb",
    )
    sid = series_id(CALENDAR_DOMAIN, CALENDAR_CODE, "session")
    rows = []
    for raw in df["date"]:
        iso = _int_date_to_iso(raw)
        if iso:
            rows.append({"series_id": sid, "trade_date": iso, "value": 1.0})
    upsert_points_long(conn, pd.DataFrame(rows))
    dates = pd.to_datetime([r["trade_date"] for r in rows])
    update_sync_meta(
        conn,
        dataset,
        source="stockdb",
        row_count=len(rows),
        min_date=dates.min().date() if len(dates) else None,
        max_date=dates.max().date() if len(dates) else None,
    )
    return f"  交易日历: {len(rows)} 天（源自 {CALENDAR_CODE}）"


def sync_stock_daily(
    codes: list[str] | None = None,
    *,
    batch_size: int = 120,
    force: bool = False,
    scope: str = "all",
) -> list[str]:
    ensure_schema()
    conn = get_connection()
    client = _get_stockdb_client()
    source = "stockdb"
    if codes is None:
        codes, source = _resolve_stock_codes(conn, scope)
    if not codes:
        return ["  个股日K: 未获取到股票列表"]

    lines: list[str] = [f"  范围: {scope}，共 {len(codes)} 只（来源 {source}）"]
    print(f"  预注册 series…", flush=True)
    _ensure_all_series_registered(conn, codes)

    today = date.today().strftime("%Y%m%d")
    total_points = 0

    for i in range(0, len(codes), batch_size):
        batch = codes[i : i + batch_size]

        if force:
            last_dates: dict[str, date] = {}
        else:
            last_dates = max_trade_dates_batch(
                conn, batch, domain=STOCK_DOMAIN, field_name="close"
            )

        groups: dict[str | None, list[str]] = {}
        for code in batch:
            if force or code not in last_dates:
                groups.setdefault(None, []).append(code)
            else:
                start = (last_dates[code] + timedelta(days=1)).strftime("%Y%m%d")
                if start > today:
                    continue
                groups.setdefault(start, []).append(code)

        batch_points = 0
        batch_frames: list[pd.DataFrame] = []
        for start, group_codes in groups.items():
            if not group_codes:
                continue
            label = start or "全历史"
            print(f"  拉取 {len(group_codes)} 只 ({label})…", flush=True)
            try:
                kline_dict = _fetch_kline_split(client, group_codes, start, today)
            except Exception as exc:
                lines.append(f"  批次 {group_codes[:3]}… 拉取失败: {exc}")
                continue

            points = _kline_dict_to_points(kline_dict)
            if points.empty:
                continue
            n = upsert_points_long(conn, points)
            batch_points += n
            batch_frames.append(points)

        if batch_frames:
            batch_all = pd.concat(batch_frames, ignore_index=True)
            bulk_update_sync_meta_from_points(
                conn, batch_all, domain=STOCK_DOMAIN, source="stockdb"
            )

        total_points += batch_points
        done = min(i + batch_size, len(codes))
        print(f"  个股日K: {done}/{len(codes)}，本批写入 {batch_points} 点", flush=True)

    lines.append(f"  个股日K: 本次写入 {total_points} 时点")
    lines.append(f"  stock_daily 累计: {point_count(conn, domain=STOCK_DOMAIN)} 点")
    return lines


def sync_stock_qfq(
    codes: list[str] | None = None,
    *,
    batch_size: int = 120,
    force: bool = False,
    scope: str = "all",
) -> list[str]:
    """同步前复权收盘价（stock_daily_qfq:close）。"""
    ensure_schema()
    conn = get_connection()
    client = _get_stockdb_client()
    source = "stockdb"
    if codes is None:
        codes, source = _resolve_stock_codes(conn, scope)
    if not codes:
        return ["  个股 qfq: 未获取到股票列表"]

    lines: list[str] = [f"  qfq 范围: {scope}，共 {len(codes)} 只（来源 {source}）"]
    print("  预注册 qfq series…", flush=True)
    total = len(codes)
    for i in range(0, total, REGISTER_CHUNK):
        chunk = codes[i : i + REGISTER_CHUNK]
        _ensure_qfq_series_registered(conn, chunk)
        done = min(i + REGISTER_CHUNK, total)
        if done == total or done % 2000 == 0 or i == 0:
            print(f"  预注册 qfq: {done}/{total}", flush=True)

    today = date.today().strftime("%Y%m%d")
    total_points = 0

    for i in range(0, len(codes), batch_size):
        batch = codes[i : i + batch_size]
        if force:
            last_dates: dict[str, date] = {}
        else:
            last_dates = max_trade_dates_batch(
                conn, batch, domain=STOCK_QFQ_DOMAIN, field_name="close"
            )

        groups: dict[str | None, list[str]] = {}
        for code in batch:
            if force or code not in last_dates:
                groups.setdefault(None, []).append(code)
            else:
                start = (last_dates[code] + timedelta(days=1)).strftime("%Y%m%d")
                if start > today:
                    continue
                groups.setdefault(start, []).append(code)

        batch_points = 0
        batch_frames: list[pd.DataFrame] = []
        for start, group_codes in groups.items():
            if not group_codes:
                continue
            label = start or "全历史"
            print(f"  拉取 qfq {len(group_codes)} 只 ({label})…", flush=True)
            try:
                points = _fetch_kline_qfq_split(client, group_codes, start, today)
            except Exception as exc:
                lines.append(f"  qfq 批次 {group_codes[:3]}… 失败: {exc}")
                continue
            if points is None or points.empty:
                continue
            n = upsert_points_long(conn, points)
            batch_points += n
            batch_frames.append(points)

        if batch_frames:
            batch_all = pd.concat(batch_frames, ignore_index=True)
            bulk_update_sync_meta_from_points(
                conn, batch_all, domain=STOCK_QFQ_DOMAIN, source="stockdb_qfq"
            )

        total_points += batch_points
        done = min(i + batch_size, len(codes))
        print(f"  个股 qfq: {done}/{len(codes)}，本批写入 {batch_points} 点", flush=True)

    lines.append(f"  个股 qfq: 本次写入 {total_points} 时点")
    lines.append(f"  stock_daily_qfq 累计: {point_count(conn, domain=STOCK_QFQ_DOMAIN)} 点")
    return lines


def sync_all(
    *,
    force: bool = False,
    stocks: bool = True,
    qfq: bool = True,
    scope: str = "all",
) -> None:
    print("StockDB → DuckDB 同步…", flush=True)
    print(sync_trade_calendar(force=force), flush=True)
    if stocks:
        for line in sync_stock_daily(force=force, scope=scope):
            print(line, flush=True)
    if qfq:
        print("\n[前复权 close]", flush=True)
        for line in sync_stock_qfq(force=force, scope=scope):
            print(line, flush=True)
    print(f"\n数据库: {ensure_schema()}", flush=True)


def main(argv=None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="从 StockDB 同步数据到 data/market.duckdb")
    parser.add_argument("--force", action="store_true", help="全量重拉（忽略已有数据）")
    parser.add_argument("--calendar-only", action="store_true", help="仅同步交易日历")
    parser.add_argument(
        "--fetch-codes-only",
        action="store_true",
        help="仅拉取并缓存 StockDB 股票列表到 DuckDB",
    )
    parser.add_argument("--codes", nargs="*", help="指定股票代码列表")
    parser.add_argument(
        "--scope",
        choices=("a_share", "all"),
        default="all",
        help="all=全部代码（默认）；a_share=仅 0/3/6/9 类 A 股",
    )
    parser.add_argument("--batch-size", type=int, default=120, help="每批股票数")
    parser.add_argument("--qfq-only", action="store_true", help="仅同步前复权 close")
    parser.add_argument("--no-qfq", action="store_true", help="不同步前复权 close")
    args = parser.parse_args(argv)
    try:
        if args.fetch_codes_only:
            ensure_schema()
            conn = get_connection()
            codes, source = _resolve_stock_codes(conn, args.scope)
            print(f"股票列表已缓存: {len(codes)} 只（来源 {source}）", flush=True)
            return 0
        if args.calendar_only:
            print(sync_trade_calendar(force=args.force), flush=True)
        elif args.qfq_only:
            for line in sync_stock_qfq(
                codes=args.codes,
                force=args.force,
                scope=args.scope,
                batch_size=args.batch_size,
            ):
                print(line, flush=True)
        else:
            print(sync_trade_calendar(force=args.force), flush=True)
            if not args.qfq_only:
                for line in sync_stock_daily(
                    codes=args.codes,
                    force=args.force,
                    scope=args.scope,
                    batch_size=args.batch_size,
                ):
                    print(line, flush=True)
            if not args.no_qfq:
                print("\n[前复权 close]", flush=True)
                for line in sync_stock_qfq(
                    codes=args.codes,
                    force=args.force,
                    scope=args.scope,
                    batch_size=args.batch_size,
                ):
                    print(line, flush=True)
    except Exception as exc:
        print(f"同步失败: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
