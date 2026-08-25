"""Inspect snapshot coverage, statistics and synchronization state."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Sequence

try:
    from factor_snapshot_builder import (
        DEFAULT_OUTPUT,
        FACTOR_ROOT,
        changed_partition_years,
        load_snapshot_manifest,
        load_source_manifest,
        registered_factors,
        snapshot_dates,
        source_partition_fingerprints,
    )
    from factor_snapshot_loader import load_snapshot
except ImportError:  # Imported as scripts.factor_snapshot_inspector.
    from scripts.factor_snapshot_builder import (
        DEFAULT_OUTPUT,
        FACTOR_ROOT,
        changed_partition_years,
        load_snapshot_manifest,
        load_source_manifest,
        registered_factors,
        snapshot_dates,
        source_partition_fingerprints,
    )
    from scripts.factor_snapshot_loader import load_snapshot


def _date_arg(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期必须为 YYYY-MM-DD：{value}") from exc


def show_date_range(output: Path) -> None:
    dates = snapshot_dates(output)
    if not dates:
        print("尚无 snapshot。")
        return
    print(f"日期范围：{dates[0].isoformat()} ~ {dates[-1].isoformat()}")
    print(f"Snapshot 数量：{len(dates):,}")


def show_coverage(output: Path, target_date: date) -> None:
    frame = load_snapshot(target_date, snapshot_root=output)
    total = len(frame)
    print(f"日期：{target_date.isoformat()}；股票数：{total:,}")
    print("因子\t非空数\t空值数\t覆盖率")
    for factor in frame.columns[1:]:
        valid = int(frame[factor].notna().sum())
        missing = total - valid
        coverage = valid / total if total else 0.0
        print(f"{factor}\t{valid:,}\t{missing:,}\t{coverage:.2%}")


def show_stats(output: Path, target_date: date) -> None:
    frame = load_snapshot(target_date, snapshot_root=output)
    path = output / f"trade_date={target_date.isoformat()}" / "factors.parquet"
    duplicate_symbols = int(frame["symbol"].duplicated().sum())
    print(f"日期：{target_date.isoformat()}")
    print(f"文件：{path}")
    print(f"文件大小：{path.stat().st_size / 1024:.2f} KiB")
    print(f"行数：{len(frame):,}；重复 symbol：{duplicate_symbols:,}")
    if len(frame.columns) > 1:
        stats = frame.iloc[:, 1:].describe().transpose()
        print(stats.to_string(float_format=lambda value: f"{value:.6g}"))


def show_status(output: Path) -> None:
    source_manifest = load_source_manifest(FACTOR_ROOT)
    snapshot_manifest = load_snapshot_manifest(output)
    factors = registered_factors(source_manifest)
    fingerprints = source_partition_fingerprints(factors, FACTOR_ROOT)
    changed = changed_partition_years(
        factors, source_manifest, snapshot_manifest, fingerprints
    )
    dates = snapshot_dates(output)
    manifest_count = snapshot_manifest.get("total_snapshots", 0)
    source_updated = source_manifest.get("updated_at")
    snapshot_updated = snapshot_manifest.get("updated_at")
    print(f"源 manifest 更新时间：{source_updated}")
    print(f"Snapshot manifest 更新时间：{snapshot_updated}")
    print(f"因子：{', '.join(str(value) for value in snapshot_manifest.get('factors', []))}")
    print(f"实际/manifest Snapshot 数：{len(dates):,}/{int(manifest_count):,}")
    if changed:
        print(f"需要增量重建的年度：{', '.join(str(value) for value in sorted(changed))}")
    elif len(dates) != int(manifest_count):
        print("状态：文件数与 manifest 不一致，建议运行 --incremental")
    else:
        print("状态：已同步")
    last_build = snapshot_manifest.get("last_build")
    if isinstance(last_build, dict):
        print("最近构建：" + json.dumps(last_build, ensure_ascii=False))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查横截面因子 Snapshot")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--date-range", action="store_true", help="查看覆盖日期范围")
    actions.add_argument("--coverage", type=_date_arg, metavar="DATE", help="查看某日因子覆盖率")
    actions.add_argument("--stats", type=_date_arg, metavar="DATE", help="查看某日统计信息")
    actions.add_argument("--status", action="store_true", help="查看同步状态")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    output = args.output.resolve()
    if args.date_range:
        show_date_range(output)
    elif args.coverage:
        show_coverage(output, args.coverage)
    elif args.stats:
        show_stats(output, args.stats)
    else:
        show_status(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
