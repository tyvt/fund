"""Validate factor snapshots and write a Markdown acceptance report."""

from __future__ import annotations

import argparse
import gc
import math
from datetime import date, datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Sequence

import duckdb
import pyarrow.parquet as pq

try:
    from factor_snapshot_builder import (
        DEFAULT_OUTPUT,
        FACTOR_ROOT,
        _source_dates,
        load_snapshot_manifest,
        load_source_manifest,
        registered_factors,
        snapshot_dates,
    )
    from factor_snapshot_loader import load_snapshot, load_snapshots
except ImportError:  # Imported as scripts.verify_snapshots.
    from scripts.factor_snapshot_builder import (
        DEFAULT_OUTPUT,
        FACTOR_ROOT,
        _source_dates,
        load_snapshot_manifest,
        load_source_manifest,
        registered_factors,
        snapshot_dates,
    )
    from scripts.factor_snapshot_loader import load_snapshot, load_snapshots


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "output" / "snapshot_validation_report.md"


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sample_dates(values: Sequence[date], count: int) -> list[date]:
    if len(values) <= count:
        return list(values)
    if count <= 1:
        return [values[len(values) // 2]]
    indices = {round(index * (len(values) - 1) / (count - 1)) for index in range(count)}
    return [values[index] for index in sorted(indices)]


def _snapshot_row_counts(output: Path) -> tuple[dict[date, int], list[str]]:
    counts: dict[date, int] = {}
    errors: list[str] = []
    for target_date in snapshot_dates(output):
        path = output / f"trade_date={target_date.isoformat()}" / "factors.parquet"
        try:
            rows = int(pq.ParquetFile(path).metadata.num_rows)
        except Exception as exc:  # Keep validating the rest of the dataset.
            errors.append(f"{target_date.isoformat()}: {exc}")
            continue
        counts[target_date] = rows
        if rows < 1:
            errors.append(f"{target_date.isoformat()}: 空文件")
    return counts, errors


def _source_row_counts(factors: Sequence[str]) -> tuple[dict[date, int], int]:
    per_factor: list[dict[date, int]] = []
    with duckdb.connect() as con:
        con.execute("SET threads TO 4")
        for factor in factors:
            path = FACTOR_ROOT / factor / "year=*" / "*.parquet"
            rows = con.execute(
                f"SELECT trade_date::DATE, count(*) FROM read_parquet('{_sql_path(path)}', "
                "hive_partitioning=true) GROUP BY 1"
            ).fetchall()
            per_factor.append({row[0]: int(row[1]) for row in rows})
    all_dates = set().union(*(set(item) for item in per_factor)) if per_factor else set()
    universe: dict[date, int] = {}
    disagreement = 0
    for target_date in all_dates:
        values = [item.get(target_date, 0) for item in per_factor]
        universe[target_date] = max(values)
        if len(set(values)) > 1:
            disagreement += 1
    return universe, disagreement


def _source_relation(factor: str, years: Sequence[int]) -> str:
    files = [
        path
        for year in years
        for path in sorted((FACTOR_ROOT / factor / f"year={year}").glob("*.parquet"))
    ]
    identifier = _quote_identifier(factor)
    if not files:
        return (
            "SELECT CAST(NULL AS DATE) AS trade_date, CAST(NULL AS VARCHAR) AS symbol, "
            f"CAST(NULL AS DOUBLE) AS {identifier} WHERE false"
        )
    paths = ", ".join(f"'{_sql_path(path)}'" for path in files)
    return (
        "SELECT trade_date::DATE AS trade_date, symbol::VARCHAR AS symbol, "
        f"value::DOUBLE AS {identifier} FROM read_parquet([{paths}], "
        "hive_partitioning=true)"
    )


def _sample_consistency(
    output: Path, factors: Sequence[str], dates: Sequence[date]
) -> dict[str, int]:
    years = sorted({value.year for value in dates})
    date_sql = ", ".join(f"DATE '{value.isoformat()}'" for value in dates)
    ctes: list[str] = []
    aliases: list[str] = []
    for index, factor in enumerate(factors):
        alias = f"factor_{index}"
        aliases.append(alias)
        ctes.append(
            f"{alias} AS (SELECT * FROM ({_source_relation(factor, years)}) "
            f"WHERE trade_date IN ({date_sql}))"
        )
    joined = aliases[0]
    for alias in aliases[1:]:
        joined += f" FULL OUTER JOIN {alias} USING (trade_date, symbol)"
    columns = ", ".join(_quote_identifier(name) for name in factors)
    ctes.append(f"source_wide AS (SELECT trade_date, symbol, {columns} FROM {joined})")

    snapshot_files = [
        output / f"trade_date={value.isoformat()}" / "factors.parquet" for value in dates
    ]
    paths = ", ".join(f"'{_sql_path(path)}'" for path in snapshot_files)
    ctes.append(
        "snapshot AS (SELECT trade_date::DATE AS trade_date, symbol::VARCHAR AS symbol, "
        f"{columns} FROM read_parquet([{paths}], hive_partitioning=true, union_by_name=true))"
    )
    mismatches: list[str] = []
    for factor in factors:
        identifier = _quote_identifier(factor)
        mismatches.append(
            "CASE WHEN (src.{0} IS NULL) <> (snap.{0} IS NULL) "
            "OR (src.{0} IS NOT NULL AND snap.{0} IS NOT NULL "
            "AND abs(src.{0} - snap.{0}) >= 1e-9) THEN 1 ELSE 0 END".format(identifier)
        )
    mismatch_sum = " + ".join(mismatches) if mismatches else "0"
    query = f"""
WITH {', '.join(ctes)},
comparison AS (
    SELECT src.symbol AS source_symbol, snap.symbol AS snapshot_symbol,
           {mismatch_sum} AS value_mismatches
    FROM source_wide src
    FULL OUTER JOIN snapshot snap USING (trade_date, symbol)
)
SELECT
    count(*) AS joined_rows,
    count(*) FILTER (WHERE source_symbol IS NULL) AS extra_snapshot_rows,
    count(*) FILTER (WHERE snapshot_symbol IS NULL) AS missing_snapshot_rows,
    coalesce(sum(value_mismatches), 0) AS value_mismatches
FROM comparison
"""
    duplicate_query = f"""
WITH snapshot AS (
    SELECT trade_date::DATE AS trade_date, symbol::VARCHAR AS symbol
    FROM read_parquet([{paths}], hive_partitioning=true, union_by_name=true)
)
SELECT coalesce(sum(rows - symbols), 0)
FROM (
    SELECT trade_date, count(*) AS rows, count(DISTINCT symbol) AS symbols
    FROM snapshot GROUP BY trade_date
)
"""
    with duckdb.connect() as con:
        con.execute("SET threads TO 4")
        row = con.execute(query).fetchone()
        duplicates = int(con.execute(duplicate_query).fetchone()[0])
    return {
        "joined_rows": int(row[0]),
        "extra_rows": int(row[1]),
        "missing_rows": int(row[2]),
        "value_checks": int(row[0]) * len(factors),
        "value_mismatches": int(row[3]),
        "duplicate_symbols": duplicates,
    }


def _performance(output: Path, dates: Sequence[date]) -> dict[str, object]:
    preferred = date(2024, 12, 31)
    single_date = preferred if preferred in dates else dates[len(dates) // 2]
    started = perf_counter()
    single = load_snapshot(single_date, snapshot_root=output)
    single_seconds = perf_counter() - started
    single_rows = len(single)
    del single
    gc.collect()

    range_start = max(date(2020, 1, 1), dates[0])
    range_end = min(date(2024, 12, 31), dates[-1])
    if range_start <= range_end:
        started = perf_counter()
        multiple = load_snapshots(
            start=range_start, end=range_end, snapshot_root=output
        )
        multiple_seconds = perf_counter() - started
        multiple_rows = len(multiple)
        del multiple
        gc.collect()
    else:
        multiple_seconds = math.nan
        multiple_rows = 0
    return {
        "single_date": single_date,
        "single_seconds": single_seconds,
        "single_rows": single_rows,
        "range_start": range_start,
        "range_end": range_end,
        "multiple_seconds": multiple_seconds,
        "multiple_rows": multiple_rows,
    }


def _status(ok: bool) -> str:
    return "✅" if ok else "❌"


def _write_report(
    report: Path,
    *,
    factors: Sequence[str],
    dates: Sequence[date],
    file_errors: Sequence[str],
    missing_dates: Sequence[date],
    extra_dates: Sequence[date],
    row_mismatches: Sequence[date],
    source_disagreement: int,
    consistency: dict[str, int],
    performance: dict[str, object],
    build_seconds: float | None,
    passed: bool,
) -> None:
    single_seconds = float(performance["single_seconds"])
    multiple_seconds = float(performance["multiple_seconds"])
    build_text = f"{build_seconds / 60:.2f} 分钟" if build_seconds is not None else "无全量构建记录"
    build_ok = build_seconds is None or build_seconds < 1800
    multiple_ok = math.isnan(multiple_seconds) or multiple_seconds < 3.0
    lines = [
        "# 横截面因子宽表验收报告",
        "",
        f"- 验收时间：{datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')}",
        f"- 因子数量：{len(factors)}",
        f"- 日期范围：{dates[0].isoformat()} ~ {dates[-1].isoformat()}",
        f"- Snapshot 数量：{len(dates):,}",
        "",
        "## 性能测试",
        "",
        "| 操作 | 耗时 | 目标 | 状态 |",
        "|---|---:|---:|:---:|",
        f"| load_snapshot('{performance['single_date']}') | {single_seconds:.3f} 秒 | < 0.5 秒 | {_status(single_seconds < 0.5)} |",
        f"| load_snapshots({performance['range_start']} ~ {performance['range_end']}) | {multiple_seconds:.3f} 秒 | < 3 秒 | {_status(multiple_ok)} |",
        f"| 全量构建 | {build_text} | < 30 分钟 | {_status(build_ok)} |",
        "",
        "## 数据一致性",
        "",
        f"- 日期缺失/多余：{len(missing_dates):,}/{len(extra_dates):,}",
        f"- 全日期股票数不一致：{len(row_mismatches):,}",
        f"- 源因子主键集合存在分歧的日期：{source_disagreement:,}",
        f"- 空文件或损坏文件：{len(file_errors):,}",
        f"- 抽检值：{consistency['value_checks']:,} 个；不一致：{consistency['value_mismatches']:,}",
        f"- 抽检日期缺失/多余行：{consistency['missing_rows']:,}/{consistency['extra_rows']:,}",
        f"- 抽检日期重复 symbol：{consistency['duplicate_symbols']:,}",
    ]
    if file_errors:
        lines.extend(["", "### 文件错误", ""])
        lines.extend(f"- {value}" for value in file_errors[:20])
    lines.extend(
        [
            "",
            "## 结论",
            "",
            "✅ 验收通过，可投入使用。" if passed else "❌ 验收未通过，请处理上述失败项。",
            "",
        ]
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验收横截面因子 Snapshot")
    parser.add_argument("--full", action="store_true", help="抽检 10 个日期并执行完整性能测试")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    output = args.output.resolve()
    report = args.report.resolve()
    source_manifest = load_source_manifest(FACTOR_ROOT)
    snapshot_manifest = load_snapshot_manifest(output)
    source_factors = registered_factors(source_manifest)
    raw_snapshot_factors = snapshot_manifest.get("factors", [])
    snapshot_factors = (
        [str(value) for value in raw_snapshot_factors]
        if isinstance(raw_snapshot_factors, list)
        else []
    )
    factors = [name for name in source_factors if name in snapshot_factors]
    dates = snapshot_dates(output)
    if not dates or not factors:
        raise SystemExit("没有可验收的 Snapshot 或因子")

    print("检查日期集合与全部文件行数...", flush=True)
    source_dates = _source_dates(factors, FACTOR_ROOT)
    source_counts, source_disagreement = _source_row_counts(factors)
    snapshot_counts, file_errors = _snapshot_row_counts(output)
    missing_dates = sorted(set(source_dates) - set(dates))
    extra_dates = sorted(set(dates) - set(source_dates))
    row_mismatches = sorted(
        target_date
        for target_date in set(source_counts) | set(snapshot_counts)
        if source_counts.get(target_date) != snapshot_counts.get(target_date)
    )

    sampled = _sample_dates(dates, 10 if args.full else 3)
    print(f"抽检 {len(sampled)} 个日期的所有因子值...", flush=True)
    consistency = _sample_consistency(output, factors, sampled)
    print("执行查询性能测试...", flush=True)
    performance = _performance(output, dates)
    last_build = snapshot_manifest.get("last_full_build", snapshot_manifest.get("last_build", {}))
    build_seconds: float | None = None
    if isinstance(last_build, dict) and last_build.get("mode") == "full":
        raw_seconds = last_build.get("elapsed_seconds")
        if isinstance(raw_seconds, (int, float)):
            build_seconds = float(raw_seconds)

    multiple_seconds = float(performance["multiple_seconds"])
    performance_ok = (
        float(performance["single_seconds"]) < 0.5
        and (math.isnan(multiple_seconds) or multiple_seconds < 3.0)
        and (build_seconds is None or build_seconds < 1800)
    )
    data_ok = not any(
        [
            file_errors,
            missing_dates,
            extra_dates,
            row_mismatches,
            source_disagreement,
            consistency["extra_rows"],
            consistency["missing_rows"],
            consistency["value_mismatches"],
            consistency["duplicate_symbols"],
        ]
    )
    passed = data_ok and performance_ok
    _write_report(
        report,
        factors=factors,
        dates=dates,
        file_errors=file_errors,
        missing_dates=missing_dates,
        extra_dates=extra_dates,
        row_mismatches=row_mismatches,
        source_disagreement=source_disagreement,
        consistency=consistency,
        performance=performance,
        build_seconds=build_seconds,
        passed=passed,
    )
    print(f"验收报告：{report}", flush=True)
    print("验收通过。" if passed else "验收失败。", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
