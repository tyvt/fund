# -*- coding: utf-8 -*-

"""从 DuckDB 读取行情：个股日 K、交易日历、股票列表。"""



from __future__ import annotations



from datetime import date



import pandas as pd



from config import MARKET_DUCKDB_PATH

from duckdb_cache import ensure_duckdb_cache_ready

from duckdb_store import (

    STOCK_DAILY_FIELDS,

    STOCK_QFQ_DOMAIN,

    get_connection,

    load_kv_snapshot,

    load_wide_frame,

    series_id,

)

from dividend_lowvol_rotation.symbols import normalize_stock_code



STOCK_DOMAIN = "stock_daily"

CALENDAR_DOMAIN = "trade_calendar"

CALENDAR_CODE = "000001"

STOCK_CODES_SNAPSHOT = "stockdb:股票代码"

A_SHARE_CATEGORIES = ("0", "3", "6", "9")





def duckdb_available() -> bool:

    return ensure_duckdb_cache_ready()





def _use_qfq_domain(fq: str | None) -> bool:

    if fq is None:

        return True

    return str(fq).lower() in ("qfq", "forward", "1", "true")





def list_a_share_codes(*, scope: str = "all") -> list[str]:

    """从 DuckDB 快照或已注册 series 读取股票列表。"""

    if not duckdb_available():

        return []

    conn = get_connection(read_only=True)

    try:

        payload = load_kv_snapshot(conn, STOCK_CODES_SNAPSHOT)

        if isinstance(payload, dict):

            keys = payload.keys() if scope == "all" else A_SHARE_CATEGORIES

            codes: list[str] = []

            for key in keys:

                items = payload.get(key)

                if isinstance(items, list):

                    codes.extend(normalize_stock_code(str(c)) for c in items)

            if codes:

                return sorted(set(codes))

        rows = conn.execute(

            "SELECT DISTINCT entity_key FROM ts_series WHERE domain = ? ORDER BY 1",

            [STOCK_DOMAIN],

        ).fetchall()

        return [normalize_stock_code(str(r[0])) for r in rows if r and r[0]]

    finally:

        conn.close()





def load_trade_calendar(

    start: str | date,

    end: str | date,

) -> list[pd.Timestamp]:

    if not duckdb_available():

        return []

    sid = series_id(CALENDAR_DOMAIN, CALENDAR_CODE, "session")

    conn = get_connection(read_only=True)

    try:

        rows = conn.execute(

            """

            SELECT trade_date FROM ts_point

            WHERE series_id = ? AND trade_date >= ? AND trade_date <= ?

            ORDER BY trade_date

            """,

            [sid, pd.Timestamp(start).date(), pd.Timestamp(end).date()],

        ).fetchall()

        return [pd.Timestamp(r[0]) for r in rows if r and r[0]]

    finally:

        conn.close()





def load_stock_kline(

    code: str,

    start: str,

    end: str,

    *,

    fields: tuple[str, ...] = ("close",),

    fq: str | None = None,

) -> pd.DataFrame | None:

    codes = batch_load_stock_klines([code], start, end, fields=fields, fq=fq)

    df = codes.get(normalize_stock_code(code))

    return df if df is not None and not df.empty else None





def batch_load_stock_klines(

    codes: list[str],

    start: str,

    end: str,

    *,

    fields: tuple[str, ...] = ("close",),

    fq: str | None = None,

) -> dict[str, pd.DataFrame]:

    """批量读取个股日 K（宽表 date + fields）。qfq → stock_daily_qfq；否则 stock_daily。"""

    if not codes or not duckdb_available():

        return {}



    norm = [normalize_stock_code(c) for c in codes]

    norm = list(dict.fromkeys(norm))

    use_qfq = _use_qfq_domain(fq)

    if use_qfq:

        domain = STOCK_QFQ_DOMAIN

        field_list = ["close"]

    else:

        domain = STOCK_DOMAIN

        field_list = [f for f in fields if f in STOCK_DAILY_FIELDS]

        if not field_list:

            field_list = ["close"]



    start_d = pd.Timestamp(start).date()

    end_d = pd.Timestamp(end).date()

    conn = get_connection(read_only=True)

    try:

        code_ph = ", ".join("?" for _ in norm)

        field_ph = ", ".join("?" for _ in field_list)

        sql = f"""

            SELECT s.entity_key, s.field_name, p.trade_date, p.value

            FROM ts_point p

            INNER JOIN ts_series s ON p.series_id = s.series_id

            WHERE s.domain = ?

              AND s.entity_key IN ({code_ph})

              AND s.field_name IN ({field_ph})

              AND p.trade_date >= ? AND p.trade_date <= ?

            ORDER BY s.entity_key, p.trade_date

        """

        params: list = [domain, *norm, *field_list, start_d, end_d]

        long_df = conn.execute(sql, params).fetchdf()

    finally:

        conn.close()



    if long_df is None or long_df.empty:

        return {}



    out: dict[str, pd.DataFrame] = {}

    for code, grp in long_df.groupby("entity_key"):

        if len(field_list) == 1:

            wide = grp[["trade_date", "value"]].rename(columns={"value": field_list[0]})

            wide = wide.rename(columns={"trade_date": "date"})

        else:

            wide = grp.pivot(index="trade_date", columns="field_name", values="value")

            wide = wide.reset_index().rename(columns={"trade_date": "date"})

        wide["date"] = pd.to_datetime(wide["date"])

        wide = wide.sort_values("date").reset_index(drop=True)

        out[str(code)] = wide

    return out





def load_index_kline(

    index_code: str,

    start: str,

    end: str,

    *,

    fields: tuple[str, ...] = ("close",),

) -> pd.DataFrame | None:

    """指数行情：优先 cn_index_perf。"""

    if not duckdb_available():

        return None

    conn = get_connection(read_only=True)

    try:

        for code in (index_code, f"{index_code}_20150101_20260728"):

            df = load_wide_frame(

                conn,

                domain="cn_index_perf",

                entity_key=code,

                fields=fields,

                start_date=start,

                end_date=end,

            )

            if df is not None and not df.empty:

                return df

    finally:

        conn.close()

    return None


