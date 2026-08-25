"""Query helpers for the local factor mart."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import duckdb
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
FACTOR_ROOT = ROOT / "data" / "parquet" / "factors"
MANIFEST_PATH = FACTOR_ROOT / "manifest.json"
_SAFE_FACTOR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _load_manifest() -> dict[str, object]:
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"因子注册表不存在：{MANIFEST_PATH}")
    with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _validate_factor_name(name: str, manifest: dict[str, object]) -> None:
    factors = manifest.get("factors", {})
    if not isinstance(factors, dict) or name not in factors:
        available = ", ".join(factors) if isinstance(factors, dict) else ""
        raise ValueError(f"未注册因子 {name!r}；可选：{available}")
    if not _SAFE_FACTOR_NAME.fullmatch(name):
        raise ValueError(f"因子名不安全：{name!r}")


def _normalize_date(value: str | date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"日期必须为 YYYY-MM-DD：{value}") from exc


def _normalize_symbols(symbols: Sequence[str] | None) -> list[str] | None:
    if symbols is None:
        return None
    normalized = sorted({str(symbol).strip().zfill(6) for symbol in symbols})
    return normalized


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _relation_sql(
    factor_name: str,
    *,
    start: date | None,
    end: date | None,
    symbols: Sequence[str] | None,
    params: list[object],
) -> str:
    files = list((FACTOR_ROOT / factor_name).glob("year=*/*.parquet"))
    if files:
        source = f"""
        SELECT trade_date::DATE AS trade_date, symbol::VARCHAR AS symbol, value::DOUBLE AS value
        FROM read_parquet('{_sql_path(FACTOR_ROOT / factor_name / 'year=*' / '*.parquet')}',
                          hive_partitioning=true)
        """
    else:
        source = """
        SELECT CAST(NULL AS DATE) AS trade_date,
               CAST(NULL AS VARCHAR) AS symbol,
               CAST(NULL AS DOUBLE) AS value
        WHERE false
        """

    predicates: list[str] = []
    if start is not None:
        predicates.append("trade_date >= ?")
        params.append(start)
    if end is not None:
        predicates.append("trade_date <= ?")
        params.append(end)
    if symbols is not None:
        if not symbols:
            predicates.append("false")
        else:
            predicates.append("symbol IN (" + ", ".join("?" for _ in symbols) + ")")
            params.extend(symbols)
    where = "WHERE " + " AND ".join(predicates) if predicates else ""
    return f"SELECT trade_date, symbol, value FROM ({source}) source {where}"


def _fetch_dataframe(
    con: duckdb.DuckDBPyConnection, query: str, params: Sequence[object]
) -> pd.DataFrame:
    """Use DuckDB's Arrow result path to avoid a slower row-wise DataFrame conversion."""

    table = con.execute(query, list(params)).to_arrow_table()
    return table.to_pandas(date_as_object=False)


def load_factor(
    name: str,
    start: str | date | datetime | None = None,
    end: str | date | datetime | None = None,
    symbols: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Load one factor as ``trade_date | symbol | value``."""

    manifest = _load_manifest()
    _validate_factor_name(name, manifest)
    normalized_start = _normalize_date(start)
    normalized_end = _normalize_date(end)
    if normalized_start and normalized_end and normalized_start > normalized_end:
        raise ValueError("start 不能晚于 end")
    normalized_symbols = _normalize_symbols(symbols)
    params: list[object] = []
    query = _relation_sql(
        name,
        start=normalized_start,
        end=normalized_end,
        symbols=normalized_symbols,
        params=params,
    )
    with duckdb.connect() as con:
        return _fetch_dataframe(con, query, params)


def load_factors(
    names: Sequence[str],
    start: str | date | datetime | None = None,
    end: str | date | datetime | None = None,
    symbols: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Load factors into a wide table keyed by ``trade_date, symbol``."""

    if not names:
        return pd.DataFrame(columns=["trade_date", "symbol"])
    ordered_names = list(dict.fromkeys(names))
    manifest = _load_manifest()
    for name in ordered_names:
        _validate_factor_name(name, manifest)

    normalized_start = _normalize_date(start)
    normalized_end = _normalize_date(end)
    if normalized_start and normalized_end and normalized_start > normalized_end:
        raise ValueError("start 不能晚于 end")
    normalized_symbols = _normalize_symbols(symbols)

    params: list[object] = []
    ctes: list[str] = []
    aliases: list[str] = []
    for index, name in enumerate(ordered_names):
        alias = f"factor_{index}"
        aliases.append(alias)
        relation = _relation_sql(
            name,
            start=normalized_start,
            end=normalized_end,
            symbols=normalized_symbols,
            params=params,
        )
        ctes.append(f'{alias} AS (SELECT trade_date, symbol, value AS "{name}" FROM ({relation}))')

    # Builder outputs a dense panel for every factor, so an inner key join is
    # both correct for jointly available partitions and materially faster than
    # a full outer hash join over the full history.
    joined = aliases[0]
    for alias in aliases[1:]:
        joined += f" INNER JOIN {alias} USING (trade_date, symbol)"
    columns = ", ".join(f'"{name}"' for name in ordered_names)
    query = f"""
    WITH {', '.join(ctes)},
    combined AS (SELECT * FROM {joined})
    SELECT trade_date, symbol, {columns}
    FROM combined
    """
    with duckdb.connect() as con:
        return _fetch_dataframe(con, query, params)


__all__ = ["load_factor", "load_factors"]
