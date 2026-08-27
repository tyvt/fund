#!/usr/bin/env python
"""Run the audit-required moving-block bootstrap from persisted strategy NAV."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.monte_carlo_robustness import block_bootstrap_returns


OUTPUT = ROOT / "output" / "validation" / "block_bootstrap_results.json"


def safe_tag(value: str) -> str:
    tag = re.sub(r"[^0-9A-Za-z_-]+", "_", str(value)).strip("_")
    if not tag:
        raise ValueError("tag 不得为空")
    return tag


def summarize(values: np.ndarray) -> dict[str, float]:
    clean = np.asarray(values, dtype=float)
    clean = clean[np.isfinite(clean)]
    if not len(clean):
        raise ValueError("Block Bootstrap 未产生有效结果")
    return {
        "mean": float(np.mean(clean)),
        "std": float(np.std(clean, ddof=1)) if len(clean) > 1 else 0.0,
        "p05": float(np.quantile(clean, 0.05)),
        "p50": float(np.quantile(clean, 0.50)),
        "p95": float(np.quantile(clean, 0.95)),
    }


def run_block_bootstrap(
    tag: str,
    *,
    iterations: int = 1000,
    block_size: int = 3,
    threshold: float = 0.05,
    seed: int = 20260829,
    output: Path = OUTPUT,
) -> dict[str, object]:
    tag = safe_tag(tag)
    if iterations <= 0 or block_size <= 0:
        raise ValueError("iterations 和 block-size 必须大于 0")
    metrics_path = ROOT / "output" / "ablation" / f"metrics_{tag}.json"
    nav_path = ROOT / "output" / "ablation" / f"nav_series_{tag}.csv"
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    nav_frame = pd.read_csv(nav_path, index_col=0, parse_dates=True)
    nav = nav_frame["full"].astype(float)
    selected = payload["full_execution"]["selected_symbols"]
    dates = [pd.Timestamp(value) for value in sorted(selected)]
    values = block_bootstrap_returns(
        nav,
        dates,
        block_months=block_size,
        n_iter=iterations,
        seed=seed,
    )
    summary = summarize(values)
    result: dict[str, object] = {
        "tag": tag,
        "source_metrics": str(metrics_path.relative_to(ROOT)),
        "source_nav": str(nav_path.relative_to(ROOT)),
        "iterations": int(iterations),
        "block_size_months": int(block_size),
        "seed": int(seed),
        "threshold": float(threshold),
        "summary": summary,
        "passed": bool(summary["p05"] >= threshold),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="fusion_v2 Block Bootstrap 验收")
    parser.add_argument("--tag", default="fusion_v2_fixed")
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--block-size", type=int, default=3)
    parser.add_argument("--threshold", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=20260829)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    result = run_block_bootstrap(
        args.tag,
        iterations=args.iterations,
        block_size=args.block_size,
        threshold=args.threshold,
        seed=args.seed,
        output=output,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
