#!/usr/bin/env python
"""Rebuild the H30269/H20269 three-line reconciliation from baseline artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_h30269_baseline import _metric, load_benchmark, write_comparison_plot


def compare(tag: str, output: Path) -> dict[str, object]:
    nav_path = ROOT / "output/baseline" / f"nav_{tag}.csv"
    frame = pd.read_csv(nav_path, index_col="date", parse_dates=True)
    strategy = frame["strategy_nav"].astype(float)
    combined = pd.concat(
        [strategy, load_benchmark("H20269"), load_benchmark("H30269")],
        axis=1,
        join="inner",
    ).dropna()
    metrics = {column: _metric(combined[column]) for column in combined.columns}
    write_comparison_plot(combined, metrics, output)
    result: dict[str, object] = {
        "tag": tag,
        "period": {
            "start": combined.index.min().date().isoformat(),
            "end": combined.index.max().date().isoformat(),
        },
        "metrics": metrics,
        "annual_return_gap_vs_H20269": (
            metrics["strategy_nav"]["annual_return"] - metrics["H20269"]["annual_return"]
        ),
        "within_2pp": abs(
            metrics["strategy_nav"]["annual_return"] - metrics["H20269"]["annual_return"]
        ) <= 0.02,
        "output": str(output.relative_to(ROOT)),
    }
    result_path = output.with_suffix(".json")
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="H30269/H20269 三线对账")
    parser.add_argument("--tag", default="baseline_h30269")
    parser.add_argument("--benchmark", default="H20269")
    parser.add_argument("--output", type=Path, default=Path("output/baseline/vs_benchmark.png"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.benchmark != "H20269":
        raise ValueError("官方复现必须以 H20269 全收益指数为主基准")
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    result = compare(args.tag, output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if bool(result["within_2pp"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())

