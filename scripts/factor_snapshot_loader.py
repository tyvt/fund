"""Fast query helpers for date-partitioned factor snapshots."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import duckdb
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_ROOT = ROOT / "data" / "parquet" / "factors" / "snapshots"


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
    return sorted({str(symbol).strip().zfill(6) for symbol in symbols})


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _manifest(snapshot_root: Path) -> dict[str, object]:
    path = snapshot_root / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"Snapshot manifest 不存在：{path}")
    with path.open("r", encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise ValueError(f"Snapshot manifest 格式错误：{path}")
    return value


def _factor_order(
    manifest: dict[str, object], requested: Sequence[str] | None
) -> list[str]:
    raw = manifest.get("factors", [])
    available = [str(value) for value in raw] if isinstance(raw, list) else []
    if requested is None:
        return available
    selected = list(dict.fromkeys(str(value) for value in requested))
    unknown = [name for name in selected if name not in available]
    if unknown:
        raise ValueError(f"Snapshot 中不存在因子：{', '.join(unknown)}")
    return selected


def _entries(snapshot_root: Path) -> list[tuple[date, Path]]:
    result: list[tuple[date, Path]] = []
    if not snapshot_root.exists():
        return result
    for directory in snapshot_root.glob("trade_date=????-??-??"):
        path = directory / "factors.parquet"
        if not path.is_file():
            continue
        try:
            target_date = date.fromisoformat(directory.name.removeprefix("trade_date="))
        except ValueError:
            continue
        result.append((target_date, path))
    return sorted(result)


def _read_source(files: Sequence[Path]) -> str:
    paths = ", ".join(f"'{_sql_path(path)}'" for path in files)
    return f"read_parquet([{paths}], hive_partitioning=true, union_by_name=true)"


def _projection(
    con: duckdb.DuckDBPyConnection,
    source: str,
    factors: Sequence[str],
    *,
    include_date: bool,
) -> str:
    available = {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()}
    columns: list[str] = []
    if include_date:
        columns.append("trade_date::DATE AS trade_date")
    columns.append("symbol::VARCHAR AS symbol")
    for factor in factors:
        identifier = _quote_identifier(factor)
        if factor in available:
            columns.append(f"{identifier}::DOUBLE AS {identifier}")
        else:
            columns.append(f"CAST(NULL AS DOUBLE) AS {identifier}")
    return ", ".join(columns)


def _fetch(
    query: str, params: Sequence[object], *, threads: int = 4
) -> pd.DataFrame:
    with duckdb.connect() as con:
        con.execute(f"SET threads TO {max(1, threads)}")
        table = con.execute(query, list(params)).to_arrow_table()
    return table.to_pandas(date_as_object=False)


def load_snapshot(
    trade_date: str | date | datetime,
    *,
    symbols: Sequence[str] | None = None,
    factors: Sequence[str] | None = None,
    snapshot_root: str | Path = DEFAULT_SNAPSHOT_ROOT,
) -> pd.DataFrame:
    """Load one cross section as ``symbol | factor1 | factor2 | ...``."""

    root = Path(snapshot_root).resolve()
    target_date = _normalize_date(trade_date)
    assert target_date is not None
    path = root / f"trade_date={target_date.isoformat()}" / "factors.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"Snapshot 不存在：{path}")
    selected = _factor_order(_manifest(root), factors)
    normalized_symbols = _normalize_symbols(symbols)
    source = _read_source([path])
    params: list[object] = []
    predicates: list[str] = []
    if normalized_symbols is not None:
        if not normalized_symbols:
            predicates.append("false")
        else:
            predicates.append("symbol IN (" + ", ".join("?" for _ in normalized_symbols) + ")")
            params.extend(normalized_symbols)
    where = "WHERE " + " AND ".join(predicates) if predicates else ""
    with duckdb.connect() as con:
        projection = _projection(con, source, selected, include_date=False)
    query = f"SELECT {projection} FROM {source} {where} ORDER BY symbol"
    return _fetch(query, params)


def load_snapshots(
    *,
    symbols: Sequence[str] | None = None,
    start: str | date | datetime | None = None,
    end: str | date | datetime | None = None,
    factors: Sequence[str] | None = None,
    snapshot_root: str | Path = DEFAULT_SNAPSHOT_ROOT,
) -> pd.DataFrame:
    """Load a date range as ``trade_date | symbol | factor1 | ...``."""

    root = Path(snapshot_root).resolve()
    normalized_start = _normalize_date(start)
    normalized_end = _normalize_date(end)
    if normalized_start and normalized_end and normalized_start > normalized_end:
        raise ValueError("start 不能晚于 end")
    selected = _factor_order(_manifest(root), factors)
    files = [
        path
        for target_date, path in _entries(root)
        if (normalized_start is None or target_date >= normalized_start)
        and (normalized_end is None or target_date <= normalized_end)
    ]
    columns = ["trade_date", "symbol", *selected]
    if not files:
        return pd.DataFrame(columns=columns)

    normalized_symbols = _normalize_symbols(symbols)
    source = _read_source(files)
    params: list[object] = []
    predicates: list[str] = []
    if normalized_symbols is not None:
        if not normalized_symbols:
            predicates.append("false")
        else:
            predicates.append("symbol IN (" + ", ".join("?" for _ in normalized_symbols) + ")")
            params.extend(normalized_symbols)
    where = "WHERE " + " AND ".join(predicates) if predicates else ""
    with duckdb.connect() as con:
        projection = _projection(con, source, selected, include_date=True)
    query = f"SELECT {projection} FROM {source} {where} ORDER BY trade_date, symbol"
    return _fetch(query, params)


__all__ = ["load_snapshot", "load_snapshots"]
