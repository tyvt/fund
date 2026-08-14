"""DuckDB 统一时序存储：ts_series + ts_point，及 cn_index_indicator 独立表。"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from config import MARKET_DUCKDB_PATH

_CONN_LOCK = threading.Lock()
_CONN: Any | None = None

STOCK_DAILY_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
    "turnover",
    "pe_ttm",
    "pb",
    "total_mv",
    "float_mv",
    "is_st",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ts_series (
    series_id   VARCHAR PRIMARY KEY,
    domain      VARCHAR NOT NULL,
    entity_key  VARCHAR NOT NULL,
    field_name  VARCHAR NOT NULL,
    source      VARCHAR,
    frequency   VARCHAR DEFAULT 'daily',
    unit        VARCHAR,
    description VARCHAR
);

CREATE TABLE IF NOT EXISTS ts_point (
    series_id   VARCHAR NOT NULL,
    trade_date  DATE NOT NULL,
    value       DOUBLE,
    PRIMARY KEY (series_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_ts_point_series_date
    ON ts_point(series_id, trade_date);

CREATE INDEX IF NOT EXISTS idx_ts_point_date
    ON ts_point(trade_date);

CREATE TABLE IF NOT EXISTS cn_index_indicator (
    index_code       VARCHAR NOT NULL,
    trade_date       DATE NOT NULL,
    pe               DOUBLE,
    pe2              DOUBLE,
    dividend_yield   DOUBLE,
    dividend_yield2  DOUBLE,
    PRIMARY KEY (index_code, trade_date)
);

CREATE TABLE IF NOT EXISTS sync_meta (
    dataset      VARCHAR PRIMARY KEY,
    source       VARCHAR NOT NULL,
    last_sync_at TIMESTAMP,
    row_count    BIGINT,
    min_date     DATE,
    max_date     DATE
);

CREATE TABLE IF NOT EXISTS kv_snapshot (
    snapshot_key VARCHAR PRIMARY KEY,
    payload_json VARCHAR NOT NULL,
    fetched_at   TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS dlv_fhps (
    code             VARCHAR NOT NULL,
    ex_date          DATE,
    report_date      VARCHAR NOT NULL,
    name             VARCHAR,
    cash_per_10      DOUBLE,
    fhps_yield_pct   DOUBLE,
    progress         VARCHAR,
    eps              DOUBLE,
    bps              DOUBLE,
    profit_yoy_pct   DOUBLE,
    PRIMARY KEY (code, report_date, ex_date)
);

CREATE TABLE IF NOT EXISTS dlv_risk_hist (
    code                   VARCHAR NOT NULL,
    report_year            INTEGER NOT NULL,
    report_date            DATE,
    roe_pct                DOUBLE,
    debt_ratio_pct         DOUBLE,
    net_profit             DOUBLE,
    ocf_to_profit          DOUBLE,
    interest_coverage      DOUBLE,
    roe_volatility_ratio   DOUBLE,
    PRIMARY KEY (code, report_year)
);

CREATE TABLE IF NOT EXISTS dlv_industry (
    code      VARCHAR PRIMARY KEY,
    industry  VARCHAR NOT NULL,
    source    VARCHAR
);

CREATE TABLE IF NOT EXISTS dlv_liquidity (
    as_of         DATE NOT NULL,
    code          VARCHAR NOT NULL,
    avg_total_mv  DOUBLE,
    avg_amount    DOUBLE,
    obs_days      INTEGER,
    PRIMARY KEY (as_of, code)
);
"""

STOCK_QFQ_DOMAIN = "stock_daily_qfq"
STOCK_QFQ_FIELDS = ("close",)


def series_id(domain: str, entity_key: str, field_name: str) -> str:
    return f"{domain}:{entity_key}:{field_name}"


def _import_duckdb():
    try:
        import duckdb
    except ImportError as exc:
        raise ImportError(
            "未安装 duckdb，请运行: pip install duckdb"
        ) from exc
    return duckdb


def db_path(path: Path | None = None) -> Path:
    target = path or MARKET_DUCKDB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def get_connection(*, path: Path | None = None, read_only: bool = False):
    duckdb = _import_duckdb()
    db_path(path)
    conn = duckdb.connect(str(path or MARKET_DUCKDB_PATH), read_only=read_only)
    if not read_only:
        conn.execute("SET preserve_insertion_order = false")
    return conn


@contextmanager
def connection(*, path: Path | None = None, read_only: bool = False):
    global _CONN
    with _CONN_LOCK:
        if _CONN is None:
            _CONN = get_connection(path=path, read_only=read_only)
        conn = _CONN
    try:
        yield conn
    except Exception:
        with _CONN_LOCK:
            if _CONN is not None:
                try:
                    _CONN.close()
                except Exception:
                    pass
                _CONN = None
        raise


def ensure_schema(*, path: Path | None = None) -> Path:
    target = db_path(path)
    with get_connection(path=target) as conn:
        conn.execute(_SCHEMA_SQL)
    return target


def _to_date(value) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return pd.Timestamp(value).date()


def register_series(
    conn,
    *,
    domain: str,
    entity_key: str,
    field_name: str,
    source: str | None = None,
    frequency: str = "daily",
    unit: str | None = None,
    description: str | None = None,
) -> str:
    sid = series_id(domain, entity_key, field_name)
    conn.execute(
        """
        INSERT OR REPLACE INTO ts_series
            (series_id, domain, entity_key, field_name, source, frequency, unit, description)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [sid, domain, entity_key, field_name, source, frequency, unit, description],
    )
    return sid


def register_series_fields(
    conn,
    *,
    domain: str,
    entity_key: str,
    fields: Iterable[str],
    source: str | None = None,
    frequency: str = "daily",
) -> list[str]:
    return [
        register_series(
            conn,
            domain=domain,
            entity_key=entity_key,
            field_name=field,
            source=source,
            frequency=frequency,
        )
        for field in fields
    ]


def bulk_register_series_fields(
    conn,
    entity_keys: Iterable[str],
    *,
    domain: str,
    fields: Iterable[str],
    source: str | None = None,
    frequency: str = "daily",
) -> int:
    """批量注册 series，一次写入替代逐条 INSERT。"""
    field_list = list(fields)
    rows: list[dict[str, str]] = []
    for entity_key in entity_keys:
        for field in field_list:
            rows.append(
                {
                    "series_id": series_id(domain, entity_key, field),
                    "domain": domain,
                    "entity_key": str(entity_key),
                    "field_name": field,
                    "source": source,
                    "frequency": frequency,
                }
            )
    if not rows:
        return 0
    frame = pd.DataFrame(rows)
    conn.register("_bulk_series", frame)
    conn.execute(
        """
        INSERT OR REPLACE INTO ts_series
        SELECT series_id, domain, entity_key, field_name, source, frequency, NULL, NULL
        FROM _bulk_series
        """
    )
    conn.unregister("_bulk_series")
    return len(rows)


def upsert_points_long(conn, frame: pd.DataFrame) -> int:
    """frame 列：series_id, trade_date, value"""
    if frame is None or frame.empty:
        return 0
    data = frame.copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce").dt.date
    data["value"] = pd.to_numeric(data["value"], errors="coerce")
    data = data.dropna(subset=["series_id", "trade_date"])
    if data.empty:
        return 0
    out = data[["series_id", "trade_date", "value"]]
    conn.register("_upsert_points", out)
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO ts_point
            SELECT series_id, trade_date, value FROM _upsert_points
            """
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.unregister("_upsert_points")
    return len(out)


def upsert_wide_frame(
    conn,
    frame: pd.DataFrame,
    *,
    domain: str,
    entity_key: str,
    date_col: str = "date",
    fields: dict[str, str] | None = None,
    source: str | None = None,
    frequency: str = "daily",
) -> int:
    """宽表转长表写入 ts_point。fields: {df列名: field_name}，默认除 date 外全部数值列。"""
    if frame is None or frame.empty:
        return 0
    df = frame.copy()
    if date_col not in df.columns:
        return 0
    df["_trade_date"] = pd.to_datetime(df[date_col], errors="coerce").dt.date
    df = df.dropna(subset=["_trade_date"])
    if df.empty:
        return 0

    if fields is None:
        fields = {
            col: col
            for col in df.columns
            if col not in (date_col, "_trade_date")
        }

    rows: list[dict] = []
    for col, field_name in fields.items():
        if col not in df.columns:
            continue
        register_series(
            conn,
            domain=domain,
            entity_key=entity_key,
            field_name=field_name,
            source=source,
            frequency=frequency,
        )
        sid = series_id(domain, entity_key, field_name)
        values = pd.to_numeric(df[col], errors="coerce")
        for trade_date, value in zip(df["_trade_date"], values):
            if pd.isna(value):
                continue
            rows.append(
                {"series_id": sid, "trade_date": trade_date, "value": float(value)}
            )

    if not rows:
        return 0
    return upsert_points_long(conn, pd.DataFrame(rows))


def load_wide_frame(
    conn,
    *,
    domain: str,
    entity_key: str,
    fields: Iterable[str],
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    date_col: str = "date",
) -> pd.DataFrame | None:
    field_list = list(fields)
    if not field_list:
        return None

    series_ids = [series_id(domain, entity_key, f) for f in field_list]
    placeholders = ", ".join("?" for _ in series_ids)
    params: list[Any] = list(series_ids)

    where = [f"series_id IN ({placeholders})"]
    if start_date is not None:
        where.append("trade_date >= ?")
        params.append(_to_date(start_date))
    if end_date is not None:
        where.append("trade_date <= ?")
        params.append(_to_date(end_date))

    sql = f"""
        SELECT series_id, trade_date, value
        FROM ts_point
        WHERE {' AND '.join(where)}
        ORDER BY trade_date
    """
    long_df = conn.execute(sql, params).fetchdf()
    if long_df is None or long_df.empty:
        return None

    field_by_sid = {series_id(domain, entity_key, f): f for f in field_list}
    long_df["field_name"] = long_df["series_id"].map(field_by_sid)
    wide = long_df.pivot(index="trade_date", columns="field_name", values="value")
    wide = wide.reset_index().rename(columns={"trade_date": date_col})
    wide[date_col] = pd.to_datetime(wide[date_col], errors="coerce").astype("datetime64[ns]")
    return wide.sort_values(date_col).reset_index(drop=True)


def max_trade_date(conn, *, domain: str, entity_key: str, field_name: str) -> date | None:
    sid = series_id(domain, entity_key, field_name)
    row = conn.execute(
        "SELECT max(trade_date) FROM ts_point WHERE series_id = ?",
        [sid],
    ).fetchone()
    if not row or row[0] is None:
        return None
    return _to_date(row[0])


def max_trade_dates_batch(
    conn,
    entity_keys: list[str],
    *,
    domain: str,
    field_name: str,
) -> dict[str, date]:
    """批量查询各 entity 最新交易日（直接按 series_id 索引，避免 JOIN）。"""
    if not entity_keys:
        return {}
    series_ids = [series_id(domain, k, field_name) for k in entity_keys]
    placeholders = ", ".join("?" for _ in series_ids)
    rows = conn.execute(
        f"""
        SELECT series_id, max(trade_date) AS max_date
        FROM ts_point
        WHERE series_id IN ({placeholders})
        GROUP BY series_id
        """,
        series_ids,
    ).fetchall()
    out: dict[str, date] = {}
    for sid, max_date in rows:
        if max_date is None or not sid:
            continue
        parts = sid.split(":", 2)
        if len(parts) != 3:
            continue
        out[parts[1]] = _to_date(max_date)
    return out


def upsert_cn_index_indicator(conn, frame: pd.DataFrame, index_code: str) -> int:
    if frame is None or frame.empty:
        return 0
    df = frame.copy()
    if "date" in df.columns:
        df["trade_date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    elif "trade_date" in df.columns:
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.date
    else:
        return 0
    for col in ("pe", "pe2", "dividend_yield", "dividend_yield2"):
        if col not in df.columns:
            df[col] = None
        else:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    out = df[["trade_date", "pe", "pe2", "dividend_yield", "dividend_yield2"]].dropna(
        subset=["trade_date"]
    )
    if out.empty:
        return 0
    out["index_code"] = index_code
    conn.register("_cn_indicator", out)
    conn.execute(
        """
        INSERT OR REPLACE INTO cn_index_indicator
        SELECT index_code, trade_date, pe, pe2, dividend_yield, dividend_yield2
        FROM _cn_indicator
        """
    )
    conn.unregister("_cn_indicator")
    return len(out)


def load_cn_index_indicator(conn, index_code: str) -> pd.DataFrame | None:
    df = conn.execute(
        """
        SELECT trade_date AS date, pe, pe2, dividend_yield, dividend_yield2
        FROM cn_index_indicator
        WHERE index_code = ?
        ORDER BY trade_date
        """,
        [index_code],
    ).fetchdf()
    if df is None or df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def upsert_kv_snapshot(conn, key: str, payload: Any) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO kv_snapshot (snapshot_key, payload_json, fetched_at)
        VALUES (?, ?, ?)
        """,
        [key, json.dumps(payload, ensure_ascii=False), datetime.now()],
    )


def load_kv_snapshot(conn, key: str) -> Any | None:
    row = conn.execute(
        "SELECT payload_json FROM kv_snapshot WHERE snapshot_key = ?",
        [key],
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def update_sync_meta(
    conn,
    dataset: str,
    *,
    source: str,
    row_count: int | None = None,
    min_date: date | None = None,
    max_date: date | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO sync_meta
            (dataset, source, last_sync_at, row_count, min_date, max_date)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [dataset, source, datetime.now(), row_count, min_date, max_date],
    )


def bulk_update_sync_meta_from_points(
    conn,
    points: pd.DataFrame,
    *,
    domain: str,
    source: str,
) -> int:
    """从长表 points 批量更新 sync_meta（按 entity_key 聚合）。"""
    if points is None or points.empty:
        return 0
    df = points.copy()
    parts = df["series_id"].str.split(":", n=2, expand=True)
    df["entity_key"] = parts[1]
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    agg = df.groupby("entity_key", as_index=False).agg(
        row_count=("value", "count"),
        min_date=("trade_date", "min"),
        max_date=("trade_date", "max"),
    )
    if agg.empty:
        return 0
    now = datetime.now()
    meta = agg.assign(
        dataset=agg["entity_key"].map(lambda k: f"{domain}:{k}"),
        source=source,
        last_sync_at=now,
        min_date=agg["min_date"].map(lambda d: d.date() if pd.notna(d) else None),
        max_date=agg["max_date"].map(lambda d: d.date() if pd.notna(d) else None),
    )
    conn.register("_bulk_sync_meta", meta)
    conn.execute(
        """
        INSERT OR REPLACE INTO sync_meta
        SELECT dataset, source, last_sync_at, row_count, min_date, max_date
        FROM _bulk_sync_meta
        """
    )
    conn.unregister("_bulk_sync_meta")
    return len(meta)


def is_synced_today(conn, dataset: str) -> bool:
    row = conn.execute(
        "SELECT last_sync_at FROM sync_meta WHERE dataset = ?",
        [dataset],
    ).fetchone()
    if not row or row[0] is None:
        return False
    synced = row[0]
    if isinstance(synced, datetime):
        return synced.date() == date.today()
    return pd.Timestamp(synced).date() == date.today()


def _prepare_dlv_frame(df: pd.DataFrame, date_cols: tuple[str, ...] = ()) -> pd.DataFrame:
    out = df.copy()
    for col in date_cols:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce").dt.date
    return out


def upsert_dlv_fhps(conn, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    out = _prepare_dlv_frame(df, ("ex_date",))
    if "report_date" in out.columns:
        fallback = pd.to_datetime(out["report_date"].astype(str), errors="coerce").dt.date
        if "ex_date" in out.columns:
            out["ex_date"] = out["ex_date"].where(out["ex_date"].notna(), fallback)
        else:
            out["ex_date"] = fallback
    out = out.dropna(subset=["code", "report_date"])
    cols = [
        "code",
        "ex_date",
        "report_date",
        "name",
        "cash_per_10",
        "fhps_yield_pct",
        "progress",
        "eps",
        "bps",
        "profit_yoy_pct",
    ]
    for c in cols:
        if c not in out.columns:
            out[c] = None
    out = out[cols].drop_duplicates(subset=["code", "report_date", "ex_date"], keep="last")
    conn.register("_dlv_fhps", out)
    conn.execute(
        """
        INSERT OR REPLACE INTO dlv_fhps
        SELECT code, ex_date, report_date, name, cash_per_10, fhps_yield_pct,
               progress, eps, bps, profit_yoy_pct
        FROM _dlv_fhps
        """
    )
    conn.unregister("_dlv_fhps")
    return len(out)


def upsert_dlv_risk_hist(conn, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    out = _prepare_dlv_frame(df, ("report_date",))
    if "report_year" not in out.columns and "report_date" in out.columns:
        out["report_year"] = pd.to_datetime(out["report_date"], errors="coerce").dt.year
    cols = [
        "code",
        "report_year",
        "report_date",
        "roe_pct",
        "debt_ratio_pct",
        "net_profit",
        "ocf_to_profit",
        "interest_coverage",
        "roe_volatility_ratio",
    ]
    for c in cols:
        if c not in out.columns:
            out[c] = None
    out = out[cols].dropna(subset=["code", "report_year"]).drop_duplicates(
        subset=["code", "report_year"], keep="last"
    )
    if out.empty:
        return 0
    conn.register("_dlv_risk", out)
    conn.execute(
        """
        INSERT OR REPLACE INTO dlv_risk_hist
        SELECT code, report_year, report_date, roe_pct, debt_ratio_pct, net_profit,
               ocf_to_profit, interest_coverage, roe_volatility_ratio
        FROM _dlv_risk
        """
    )
    conn.unregister("_dlv_risk")
    return len(out)


def upsert_dlv_industry(conn, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    out = df.copy()
    if "code" not in out.columns:
        return 0
    if "industry" not in out.columns:
        out["industry"] = "未分类"
    if "source" not in out.columns:
        out["source"] = None
    out = out[["code", "industry", "source"]].drop_duplicates(subset=["code"], keep="last")
    conn.register("_dlv_ind", out)
    conn.execute(
        """
        INSERT OR REPLACE INTO dlv_industry
        SELECT code, industry, source FROM _dlv_ind
        """
    )
    conn.unregister("_dlv_ind")
    return len(out)


def upsert_dlv_liquidity(conn, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    out = df.copy()
    if "as_of" not in out.columns:
        return 0
    out["as_of"] = pd.to_datetime(out["as_of"], errors="coerce").dt.date
    cols = ["as_of", "code", "avg_total_mv", "avg_amount", "obs_days"]
    for c in cols:
        if c not in out.columns:
            out[c] = None
    out = out[cols].dropna(subset=["as_of", "code"]).drop_duplicates(subset=["as_of", "code"], keep="last")
    if out.empty:
        return 0
    conn.register("_dlv_liq", out)
    conn.execute(
        """
        INSERT OR REPLACE INTO dlv_liquidity
        SELECT as_of, code, avg_total_mv, avg_amount, obs_days FROM _dlv_liq
        """
    )
    conn.unregister("_dlv_liq")
    return len(out)


def point_count(conn, *, domain: str | None = None) -> int:
    if domain:
        row = conn.execute(
            """
            SELECT count(*) FROM ts_point p
            JOIN ts_series s ON p.series_id = s.series_id
            WHERE s.domain = ?
            """,
            [domain],
        ).fetchone()
    else:
        row = conn.execute("SELECT count(*) FROM ts_point").fetchone()
    return int(row[0]) if row else 0
