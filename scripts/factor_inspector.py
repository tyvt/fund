"""Command-line diagnostics for the factor mart."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Sequence

import duckdb

from factor_loader import FACTOR_ROOT, _load_manifest, _sql_path, _validate_factor_name


def _factor_files(name: str) -> list[Path]:
    return list((FACTOR_ROOT / name).glob("year=*/*.parquet"))


def _factor_source(name: str) -> str:
    return (
        f"read_parquet('{_sql_path(FACTOR_ROOT / name / 'year=*' / '*.parquet')}', "
        "hive_partitioning=true)"
    )


def list_factors(manifest: dict[str, object]) -> None:
    factors = manifest.get("factors", {})
    assert isinstance(factors, dict)
    print(f"注册表版本：{manifest.get('version')}  更新时间：{manifest.get('updated_at')}")
    print("name\tdisplay_name\tcategory\trow_count\tmin_date\tmax_date\tlast_computed")
    for name, raw_config in factors.items():
        config = raw_config if isinstance(raw_config, dict) else {}
        print(
            "\t".join(
                str(value) if value is not None else "-"
                for value in (
                    name,
                    config.get("display_name"),
                    config.get("category"),
                    config.get("row_count", 0),
                    config.get("min_date"),
                    config.get("max_date"),
                    config.get("last_computed"),
                )
            )
        )


def show_info(manifest: dict[str, object], name: str) -> None:
    _validate_factor_name(name, manifest)
    config = manifest["factors"][name]  # type: ignore[index]
    print(json.dumps(config, ensure_ascii=False, indent=2))
    files = _factor_files(name)
    print(f"partition_files: {len(files)}")
    print(f"disk_bytes: {sum(path.stat().st_size for path in files)}")


def show_coverage(manifest: dict[str, object], name: str, target_date: date) -> None:
    _validate_factor_name(name, manifest)
    if not _factor_files(name):
        print(f"{name} 尚无数据文件")
        return
    with duckdb.connect() as con:
        row = con.execute(
            f"""
            SELECT
                count(*) AS total_rows,
                count(value) AS valid_rows,
                count(*) - count(value) AS null_rows
            FROM {_factor_source(name)}
            WHERE trade_date = ?
            """,
            [target_date],
        ).fetchone()
    total, valid, nulls = map(int, row)
    coverage = valid / total if total else 0.0
    print(f"factor: {name}")
    print(f"date: {target_date.isoformat()}")
    print(f"total_rows: {total}")
    print(f"valid_rows: {valid}")
    print(f"null_rows: {nulls}")
    print(f"coverage: {coverage:.4%}")


def show_profile(manifest: dict[str, object], name: str) -> None:
    _validate_factor_name(name, manifest)
    if not _factor_files(name):
        print(f"{name} 尚无数据文件")
        return
    with duckdb.connect() as con:
        row = con.execute(
            f"""
            SELECT
                count(*) AS total_rows,
                count(value) AS valid_rows,
                min(value) AS minimum,
                quantile_cont(value, 0.01) AS p01,
                quantile_cont(value, 0.25) AS p25,
                quantile_cont(value, 0.50) AS median,
                avg(value) AS mean,
                quantile_cont(value, 0.75) AS p75,
                quantile_cont(value, 0.99) AS p99,
                max(value) AS maximum,
                stddev_samp(value) AS sample_std,
                min(trade_date) FILTER (WHERE value IS NOT NULL) AS min_date,
                max(trade_date) FILTER (WHERE value IS NOT NULL) AS max_date
            FROM {_factor_source(name)}
            """
        ).fetchone()
        columns = [item[0] for item in con.description]
    result = dict(zip(columns, row))
    total = int(result["total_rows"])
    valid = int(result["valid_rows"])
    result["null_rows"] = total - valid
    result["coverage"] = valid / total if total else 0.0
    print(f"factor: {name}")
    for key, value in result.items():
        if isinstance(value, date):
            value = value.isoformat()
        if key == "coverage":
            print(f"{key}: {value:.4%}")
        else:
            print(f"{key}: {value}")


def _date_arg(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期必须为 YYYY-MM-DD：{value}") from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查因子注册表、覆盖率和分布")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="列出注册因子")
    group.add_argument("--info", metavar="FACTOR", help="显示因子元数据")
    group.add_argument("--coverage", metavar="FACTOR", help="显示某日覆盖率")
    group.add_argument("--profile", metavar="FACTOR", help="显示全历史分布统计")
    parser.add_argument("--date", type=_date_arg, help="--coverage 所需日期")
    args = parser.parse_args(argv)
    if args.coverage and args.date is None:
        parser.error("--coverage 必须同时提供 --date YYYY-MM-DD")
    if not args.coverage and args.date is not None:
        parser.error("--date 只能与 --coverage 一起使用")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = _load_manifest()
    if args.list:
        list_factors(manifest)
    elif args.info:
        show_info(manifest, args.info)
    elif args.coverage:
        show_coverage(manifest, args.coverage, args.date)
    elif args.profile:
        show_profile(manifest, args.profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
