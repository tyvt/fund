"""Build date-partitioned, cross-sectional factor snapshots.

The source factor mart is treated as read-only.  Bulk builds operate one year at
a time and stage every Parquet file before atomically replacing its target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Iterable, Sequence

import duckdb
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
FACTOR_ROOT = ROOT / "data" / "parquet" / "factors"
DEFAULT_OUTPUT = FACTOR_ROOT / "snapshots"
SOURCE_MANIFEST = FACTOR_ROOT / "manifest.json"
SNAPSHOT_MANIFEST_NAME = "manifest.json"
CHECKPOINT_NAME = ".snapshot_build_checkpoint.json"
_FACTOR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DATE_PARTITION = re.compile(r"^trade_date=(\d{4}-\d{2}-\d{2})$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期必须为 YYYY-MM-DD：{value}") from exc


def _normalize_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"日期必须为 YYYY-MM-DD：{value}") from exc


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _quote_identifier(value: str) -> str:
    if not _FACTOR_NAME.fullmatch(value):
        raise ValueError(f"因子名不安全：{value!r}")
    return f'"{value}"'


def _load_json(path: Path, *, required: bool = False) -> dict[str, object]:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"文件不存在：{path}")
        return {}
    with path.open("r", encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path}")
    return value


def _save_json_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_source_manifest(factor_root: Path = FACTOR_ROOT) -> dict[str, object]:
    return _load_json(factor_root / "manifest.json", required=True)


def load_snapshot_manifest(output: Path = DEFAULT_OUTPUT) -> dict[str, object]:
    return _load_json(output / SNAPSHOT_MANIFEST_NAME)


def registered_factors(source_manifest: dict[str, object]) -> list[str]:
    factors = source_manifest.get("factors")
    if not isinstance(factors, dict) or not factors:
        raise ValueError("源因子 manifest 缺少非空 factors 对象")
    names = list(factors)
    for name in names:
        _quote_identifier(name)
    return names


def parse_factor_list(value: str | None, available: Sequence[str]) -> list[str]:
    if value is None:
        return list(available)
    names = list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if not names:
        raise ValueError("--factors 不能为空")
    unknown = [name for name in names if name not in available]
    if unknown:
        raise ValueError(f"未注册因子：{', '.join(unknown)}；可选：{', '.join(available)}")
    return names


def snapshot_dates(output: Path = DEFAULT_OUTPUT) -> list[date]:
    if not output.exists():
        return []
    result: list[date] = []
    for child in output.iterdir():
        if not child.is_dir():
            continue
        match = _DATE_PARTITION.fullmatch(child.name)
        if match and (child / "factors.parquet").is_file():
            result.append(date.fromisoformat(match.group(1)))
    return sorted(result)


def source_partition_fingerprints(
    factor_names: Sequence[str], factor_root: Path = FACTOR_ROOT
) -> dict[str, dict[str, str]]:
    """Fingerprint each factor/year from file names, sizes and nanosecond mtimes."""

    result: dict[str, dict[str, str]] = {}
    for factor_name in factor_names:
        years: dict[str, str] = {}
        factor_dir = factor_root / factor_name
        for year_dir in sorted(factor_dir.glob("year=*")):
            if not year_dir.is_dir() or not year_dir.name[5:].isdigit():
                continue
            digest = hashlib.sha256()
            files = sorted(year_dir.glob("*.parquet"))
            for path in files:
                stat = path.stat()
                digest.update(path.name.encode("utf-8"))
                digest.update(b"\0")
                digest.update(str(stat.st_size).encode("ascii"))
                digest.update(b"\0")
                digest.update(str(stat.st_mtime_ns).encode("ascii"))
                digest.update(b"\n")
            if files:
                years[year_dir.name[5:]] = digest.hexdigest()
        result[factor_name] = years
    return result


def changed_partition_years(
    selected_factors: Sequence[str],
    source_manifest: dict[str, object],
    snapshot_manifest: dict[str, object],
    current_fingerprints: dict[str, dict[str, str]],
) -> set[int]:
    previous_fingerprints = snapshot_manifest.get("source_partitions", {})
    previous_versions = snapshot_manifest.get("factor_versions", {})
    source_factors = source_manifest.get("factors", {})
    changed: set[int] = set()
    for factor_name in selected_factors:
        current = current_fingerprints.get(factor_name, {})
        previous = (
            previous_fingerprints.get(factor_name, {})
            if isinstance(previous_fingerprints, dict)
            else {}
        )
        if not isinstance(previous, dict):
            previous = {}
        source_config = source_factors.get(factor_name, {}) if isinstance(source_factors, dict) else {}
        current_version = source_config.get("version") if isinstance(source_config, dict) else None
        previous_version = (
            previous_versions.get(factor_name) if isinstance(previous_versions, dict) else None
        )
        year_keys = set(current) | set(previous)
        if previous_version != current_version:
            changed.update(int(year) for year in year_keys)
            continue
        for year in year_keys:
            if previous.get(year) != current.get(year):
                changed.add(int(year))
    return changed


def _source_dates(
    factor_names: Sequence[str], factor_root: Path = FACTOR_ROOT
) -> list[date]:
    relations: list[str] = []
    for factor_name in factor_names:
        files = list((factor_root / factor_name).glob("year=*/*.parquet"))
        if files:
            relations.append(
                "SELECT trade_date::DATE AS trade_date FROM "
                f"read_parquet('{_sql_path(factor_root / factor_name / 'year=*' / '*.parquet')}', "
                "hive_partitioning=true)"
            )
    if not relations:
        return []
    query = "SELECT DISTINCT trade_date FROM (" + " UNION ALL ".join(relations) + ") ORDER BY 1"
    with duckdb.connect() as con:
        return [row[0] for row in con.execute(query).fetchall()]


def _year_bounds(year: int, start: date | None, end: date | None) -> tuple[date, date]:
    lower = max(date(year, 1, 1), start) if start is not None else date(year, 1, 1)
    upper = min(date(year, 12, 31), end) if end is not None else date(year, 12, 31)
    return lower, upper


def _factor_relation(factor_root: Path, factor_name: str, year: int) -> str:
    column = _quote_identifier(factor_name)
    factor_glob = factor_root / factor_name / f"year={year}" / "*.parquet"
    if list((factor_root / factor_name / f"year={year}").glob("*.parquet")):
        return (
            "SELECT trade_date::DATE AS trade_date, symbol::VARCHAR AS symbol, "
            f"value::DOUBLE AS {column} FROM read_parquet('{_sql_path(factor_glob)}', "
            "hive_partitioning=true)"
        )
    return (
        "SELECT CAST(NULL AS DATE) AS trade_date, CAST(NULL AS VARCHAR) AS symbol, "
        f"CAST(NULL AS DOUBLE) AS {column} WHERE false"
    )


def _existing_files(output: Path, year: int, lower: date, upper: date) -> list[Path]:
    files: list[Path] = []
    if not output.exists():
        return files
    prefix = f"trade_date={year:04d}-"
    for directory in output.glob(f"{prefix}*"):
        match = _DATE_PARTITION.fullmatch(directory.name)
        if not match:
            continue
        target_date = date.fromisoformat(match.group(1))
        path = directory / "factors.parquet"
        if lower <= target_date <= upper and path.is_file() and path.stat().st_size > 0:
            files.append(path)
    return sorted(files)


def _existing_relation(
    con: duckdb.DuckDBPyConnection,
    files: Sequence[Path],
    factor_names: Sequence[str],
) -> str:
    columns = [_quote_identifier(name) for name in factor_names]
    if not files:
        nulls = ", ".join(f"CAST(NULL AS DOUBLE) AS {column}" for column in columns)
        suffix = f", {nulls}" if nulls else ""
        return (
            "SELECT CAST(NULL AS DATE) AS trade_date, CAST(NULL AS VARCHAR) AS symbol"
            f"{suffix} WHERE false"
        )
    paths = ", ".join(f"'{_sql_path(path)}'" for path in files)
    source = f"read_parquet([{paths}], hive_partitioning=true, union_by_name=true)"
    available = {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()}
    projected: list[str] = []
    for name, column in zip(factor_names, columns):
        if name in available:
            projected.append(f"{column}::DOUBLE AS {column}")
        else:
            projected.append(f"CAST(NULL AS DOUBLE) AS {column}")
    suffix = ", " + ", ".join(projected) if projected else ""
    return f"SELECT trade_date::DATE AS trade_date, symbol::VARCHAR AS symbol{suffix} FROM {source}"


def _build_year_worker(payload: dict[str, object]) -> dict[str, object]:
    factor_root = Path(str(payload["factor_root"]))
    output = Path(str(payload["output"]))
    staging = Path(str(payload["staging"]))
    year = int(payload["year"])
    lower = date.fromisoformat(str(payload["lower"]))
    upper = date.fromisoformat(str(payload["upper"]))
    selected = [str(value) for value in payload["selected"]]  # type: ignore[index]
    final_factors = [str(value) for value in payload["final_factors"]]  # type: ignore[index]
    preserved = [name for name in final_factors if name not in selected]
    worker_root = staging / f"year_{year}"
    if worker_root.exists():
        shutil.rmtree(worker_root)
    worker_root.mkdir(parents=True, exist_ok=False)

    con = duckdb.connect()
    try:
        con.execute(f"SET threads TO {int(payload['threads'])}")
        con.execute(f"SET memory_limit = '{str(payload['memory_limit'])}'")
        temp_dir = worker_root / ".duckdb_tmp"
        temp_dir.mkdir()
        con.execute(f"SET temp_directory = '{_sql_path(temp_dir)}'")
        con.execute("SET preserve_insertion_order = false")

        ctes: list[str] = []
        aliases: list[str] = []
        for index, factor_name in enumerate(selected):
            alias = f"factor_{index}"
            aliases.append(alias)
            ctes.append(f"{alias} AS ({_factor_relation(factor_root, factor_name, year)})")

        existing = _existing_files(output, year, lower, upper)
        if preserved:
            aliases.append("existing")
            ctes.append(f"existing AS ({_existing_relation(con, existing, preserved)})")
        if not aliases:
            return {"year": year, "dates": [], "rows": 0, "staging": str(worker_root)}

        joined = aliases[0]
        for alias in aliases[1:]:
            joined += f" FULL OUTER JOIN {alias} USING (trade_date, symbol)"
        columns = ", ".join(_quote_identifier(name) for name in final_factors)
        select_columns = f", {columns}" if columns else ""
        query = f"""
WITH {', '.join(ctes)}
SELECT trade_date, symbol{select_columns}
FROM {joined}
WHERE trade_date BETWEEN DATE '{lower.isoformat()}' AND DATE '{upper.isoformat()}'
"""
        con.execute(f"CREATE TEMP TABLE snapshot_result AS {query}")
        expected_rows = {
            row[0].isoformat(): int(row[1])
            for row in con.execute(
                "SELECT trade_date, count(*) FROM snapshot_result GROUP BY 1 ORDER BY 1"
            ).fetchall()
        }
        destination = worker_root / "partitions"
        con.execute(
            f"COPY (SELECT * FROM snapshot_result ORDER BY trade_date, symbol) "
            f"TO '{_sql_path(destination)}' "
            "(FORMAT PARQUET, PARTITION_BY (trade_date), COMPRESSION ZSTD, "
            "ROW_GROUP_SIZE 100000, FILENAME_PATTERN 'factors')"
        )
        partitions: list[str] = []
        rows = 0
        if destination.exists():
            for directory in sorted(destination.glob("trade_date=*")):
                match = _DATE_PARTITION.fullmatch(directory.name)
                generated = sorted(directory.glob("*.parquet"))
                if not match or not generated:
                    continue
                parquet = directory / "factors.parquet"
                if len(generated) > 1:
                    paths = ", ".join(f"'{_sql_path(path)}'" for path in generated)
                    merged = worker_root / f"merged_{match.group(1)}.parquet"
                    con.execute(
                        f"COPY (SELECT * FROM read_parquet([{paths}], union_by_name=true) "
                        f"ORDER BY symbol) TO '{_sql_path(merged)}' "
                        "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)"
                    )
                    for path in generated:
                        path.unlink()
                    os.replace(merged, parquet)
                elif generated[0] != parquet:
                    os.replace(generated[0], parquet)
                partitions.append(match.group(1))
                actual_rows = int(
                    con.execute(
                        f"SELECT num_rows FROM parquet_file_metadata('{_sql_path(parquet)}')"
                    ).fetchone()[0]
                )
                rows += actual_rows
                if actual_rows != expected_rows.get(match.group(1)):
                    raise RuntimeError(
                        f"{match.group(1)} 写入行数不一致："
                        f"expected={expected_rows.get(match.group(1))}, actual={actual_rows}"
                    )
        if set(partitions) != set(expected_rows):
            missing = sorted(set(expected_rows) - set(partitions))
            extra = sorted(set(partitions) - set(expected_rows))
            raise RuntimeError(f"{year} 日期分区写入不完整：missing={missing}, extra={extra}")
        return {"year": year, "dates": partitions, "rows": rows, "staging": str(worker_root)}
    finally:
        con.close()


def _commit_year(
    result: dict[str, object],
    output: Path,
    lower: date,
    upper: date,
    *,
    remove_stale: bool,
) -> int:
    worker_root = Path(str(result["staging"]))
    destination = worker_root / "partitions"
    built_dates = {str(value) for value in result.get("dates", [])}  # type: ignore[arg-type]
    for date_text in sorted(built_dates):
        source = destination / f"trade_date={date_text}" / "factors.parquet"
        target_dir = output / f"trade_date={date_text}"
        target_dir.mkdir(parents=True, exist_ok=True)
        os.replace(source, target_dir / "factors.parquet")

    if remove_stale:
        year = int(result["year"])
        for path in _existing_files(output, year, lower, upper):
            match = _DATE_PARTITION.fullmatch(path.parent.name)
            if match and match.group(1) not in built_dates:
                path.unlink()
                try:
                    path.parent.rmdir()
                except OSError:
                    pass
    shutil.rmtree(worker_root, ignore_errors=True)
    return len(built_dates)


def _factor_metadata(
    source_manifest: dict[str, object], names: Sequence[str], field: str
) -> dict[str, object]:
    factors = source_manifest.get("factors", {})
    result: dict[str, object] = {}
    if isinstance(factors, dict):
        for name in names:
            config = factors.get(name, {})
            result[name] = config.get(field) if isinstance(config, dict) else None
    return result


def _merge_factor_order(
    available: Sequence[str], selected: Sequence[str], snapshot_manifest: dict[str, object]
) -> list[str]:
    previous = snapshot_manifest.get("factors", [])
    previous_names = [str(value) for value in previous] if isinstance(previous, list) else []
    desired = set(previous_names) | set(selected)
    unknown_previous = sorted(desired - set(available))
    if unknown_previous:
        raise ValueError(f"宽表 manifest 包含源集市未注册因子：{', '.join(unknown_previous)}")
    return [name for name in available if name in desired]


def _checkpoint_signature(
    mode: str,
    selected: Sequence[str],
    final_factors: Sequence[str],
    work: Sequence[tuple[int, date, date]],
) -> dict[str, object]:
    return {
        "mode": mode,
        "selected": list(selected),
        "final_factors": list(final_factors),
        "work": [
            {"year": year, "start": lower.isoformat(), "end": upper.isoformat()}
            for year, lower, upper in work
        ],
    }


def build_snapshot(
    trade_date: str | date | datetime,
    factor_names: Sequence[str],
    output: str | Path = DEFAULT_OUTPUT,
) -> pd.DataFrame:
    """Build one snapshot and return ``symbol | factor1 | factor2 | ...``."""

    from factor_loader import load_factor

    target_date = _normalize_date(trade_date)
    ordered = list(dict.fromkeys(factor_names))
    if not ordered:
        raise ValueError("factor_names 不能为空")
    frames: list[pd.DataFrame] = []
    for factor_name in ordered:
        frame = load_factor(factor_name, start=target_date, end=target_date)
        frames.append(frame[["symbol", "value"]].rename(columns={"value": factor_name}))
    result = frames[0]
    for frame in frames[1:]:
        result = result.merge(frame, on="symbol", how="outer", validate="one_to_one")
    result = result[["symbol", *ordered]].sort_values("symbol", kind="stable").reset_index(drop=True)
    if result.empty:
        raise ValueError(f"源因子在 {target_date.isoformat()} 没有数据")

    output_path = Path(output).resolve() / f"trade_date={target_date.isoformat()}" / "factors.parquet"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        result.to_parquet(temporary, index=False, compression="zstd")
        os.replace(temporary, output_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建按交易日分区的横截面因子宽表")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true", help="全量构建所有年份")
    mode.add_argument("--incremental", action="store_true", help="重建新增或指纹变化的年份")
    parser.add_argument("--start", type=_parse_date, help="范围构建起始日期（含）")
    parser.add_argument("--end", type=_parse_date, help="范围构建结束日期（含）")
    parser.add_argument("--factors", help="逗号分隔的因子子集；保留宽表中的其他因子列")
    parser.add_argument("--force", action="store_true", help="强制重写目标范围")
    parser.add_argument("--resume", action="store_true", help="从匹配的年度检查点继续")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Snapshot 输出目录")
    parser.add_argument("--workers", type=int, default=min(2, os.cpu_count() or 1))
    parser.add_argument("--memory-limit", default="2GB", help="每个构建进程的 DuckDB 内存上限")
    args = parser.parse_args(argv)
    if args.start and args.end and args.start > args.end:
        parser.error("--start 不能晚于 --end")
    if args.full and (args.start or args.end):
        parser.error("--full 不能与 --start/--end 同时使用")
    if args.incremental and (args.start or args.end):
        parser.error("--incremental 不能与 --start/--end 同时使用")
    if args.force and args.resume:
        parser.error("--force 与 --resume 不能同时使用")
    if args.workers <= 0:
        parser.error("--workers 必须大于 0")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    factor_root = FACTOR_ROOT.resolve()
    output = args.output.resolve()
    source_manifest = load_source_manifest(factor_root)
    available = registered_factors(source_manifest)
    try:
        selected = parse_factor_list(args.factors, available)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    snapshot_manifest = load_snapshot_manifest(output)
    final_factors = _merge_factor_order(available, selected, snapshot_manifest)
    fingerprints = source_partition_fingerprints(selected, factor_root)
    source_years = {
        int(year)
        for factor in selected
        for year in fingerprints.get(factor, {})
    }
    existing_dates = snapshot_dates(output)
    existing_years = {value.year for value in existing_dates}

    if args.incremental:
        mode = "incremental"
        years = changed_partition_years(
            selected, source_manifest, snapshot_manifest, fingerprints
        )
        source_date_values = _source_dates(selected, factor_root)
        missing_dates = set(source_date_values) - set(existing_dates)
        years.update(value.year for value in missing_dates)
    elif args.full or (args.start is None and args.end is None):
        mode = "full"
        years = source_years | existing_years
    else:
        mode = "range"
        lower_year = args.start.year if args.start else min(source_years)
        upper_year = args.end.year if args.end else max(source_years)
        years = set(range(lower_year, upper_year + 1))

    work: list[tuple[int, date, date]] = []
    for year in sorted(years):
        lower, upper = _year_bounds(year, args.start, args.end)
        if lower <= upper:
            work.append((year, lower, upper))

    output.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output / CHECKPOINT_NAME
    signature = _checkpoint_signature(mode, selected, final_factors, work)
    completed_years: set[int] = set()
    if args.resume and checkpoint_path.exists():
        checkpoint = _load_json(checkpoint_path)
        if checkpoint.get("signature") != signature:
            raise SystemExit("现有检查点与本次参数不匹配；请去掉 --resume 重新构建")
        completed = checkpoint.get("completed_years", [])
        if isinstance(completed, list):
            completed_years = {int(value) for value in completed}
    checkpoint: dict[str, object] = {
        "version": "1.0",
        "created_at": _utc_now(),
        "signature": signature,
        "completed_years": sorted(completed_years),
    }

    pending = [item for item in work if item[0] not in completed_years]
    if not pending:
        print("没有需要构建的年度分区。", flush=True)
    else:
        _save_json_atomic(checkpoint_path, checkpoint)
        print(
            f"模式={mode}；更新因子={','.join(selected)}；年度={','.join(str(item[0]) for item in pending)}",
            flush=True,
        )

    started = perf_counter()
    build_id = uuid.uuid4().hex
    staging = output / ".staging" / build_id
    staging.mkdir(parents=True, exist_ok=False)
    written = 0
    rows = 0
    bounds = {year: (lower, upper) for year, lower, upper in work}
    remove_stale = not [name for name in final_factors if name not in selected]
    try:
        if pending:
            threads = max(
                1,
                min(4, (os.cpu_count() or 1) // min(args.workers, len(pending))),
            )
            payloads = [
                {
                    "factor_root": str(factor_root),
                    "output": str(output),
                    "staging": str(staging),
                    "year": year,
                    "lower": lower.isoformat(),
                    "upper": upper.isoformat(),
                    "selected": selected,
                    "final_factors": final_factors,
                    "threads": threads,
                    "memory_limit": args.memory_limit,
                }
                for year, lower, upper in pending
            ]
            with ProcessPoolExecutor(max_workers=min(args.workers, len(payloads))) as executor:
                futures = {executor.submit(_build_year_worker, payload): payload for payload in payloads}
                for future in as_completed(futures):
                    result = future.result()
                    year = int(result["year"])
                    lower, upper = bounds[year]
                    count = _commit_year(
                        result, output, lower, upper, remove_stale=remove_stale
                    )
                    written += count
                    rows += int(result["rows"])
                    completed_years.add(year)
                    checkpoint["completed_years"] = sorted(completed_years)
                    _save_json_atomic(checkpoint_path, checkpoint)
                    print(f"完成 {year}：{count:,} 个 snapshot，{int(result['rows']):,} 行", flush=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            staging.parent.rmdir()
        except OSError:
            pass

    elapsed = perf_counter() - started
    all_dates = snapshot_dates(output)
    previous_versions = snapshot_manifest.get("factor_versions", {})
    previous_computed = snapshot_manifest.get("factor_last_computed", {})
    versions = dict(previous_versions) if isinstance(previous_versions, dict) else {}
    last_computed = dict(previous_computed) if isinstance(previous_computed, dict) else {}
    versions.update(_factor_metadata(source_manifest, selected, "version"))
    last_computed.update(_factor_metadata(source_manifest, selected, "last_computed"))
    previous_partitions = snapshot_manifest.get("source_partitions", {})
    partitions = dict(previous_partitions) if isinstance(previous_partitions, dict) else {}
    if mode in {"full", "incremental"}:
        partitions.update(fingerprints)
    last_build_info: dict[str, object] = {
        "mode": mode,
        "elapsed_seconds": round(elapsed, 6),
        "snapshots_written": written,
        "rows_written": rows,
        "workers": min(args.workers, len(pending)) if pending else 0,
    }
    previous_full_build = snapshot_manifest.get("last_full_build")
    last_full_build = (
        last_build_info
        if mode == "full"
        else previous_full_build if isinstance(previous_full_build, dict) else None
    )
    manifest: dict[str, object] = {
        "version": "1.0",
        "updated_at": _utc_now(),
        "factors": final_factors,
        "date_range": {
            "min": all_dates[0].isoformat() if all_dates else None,
            "max": all_dates[-1].isoformat() if all_dates else None,
        },
        "total_snapshots": len(all_dates),
        "factor_versions": {name: versions.get(name) for name in final_factors},
        "factor_last_computed": {name: last_computed.get(name) for name in final_factors},
        "source_partitions": {name: partitions.get(name, {}) for name in final_factors},
        "last_build": last_build_info,
        "last_full_build": last_full_build,
    }
    _save_json_atomic(output / SNAPSHOT_MANIFEST_NAME, manifest)
    if checkpoint_path.exists():
        checkpoint_path.unlink()
    staging_parent = output / ".staging"
    if staging_parent.exists():
        shutil.rmtree(staging_parent, ignore_errors=True)
    print(
        f"构建完成：写入 {written:,} 个 snapshot，当前共 {len(all_dates):,} 个，用时 {elapsed:.2f} 秒。",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
