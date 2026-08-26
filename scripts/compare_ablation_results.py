#!/usr/bin/env python
"""Compare two ablation metric files and render a compact before/after chart."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def _load(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.is_absolute():
        target = ROOT / target
    payload = json.loads(target.read_text(encoding="utf-8"))
    if "metrics" not in payload or "full" not in payload["metrics"]:
        raise ValueError(f"缺少 metrics.full：{target}")
    return payload


def compare(before: dict[str, Any], after: dict[str, Any]) -> pd.DataFrame:
    before_full = before["metrics"]["full"]
    after_full = after["metrics"]["full"]
    rows = []
    for key in ("annual_return", "max_drawdown", "sharpe_ratio", "volatility"):
        old = float(before_full[key])
        new = float(after_full[key])
        rows.append({"metric": key, "before": old, "after": new, "change": new - old})
    return pd.DataFrame(rows).set_index("metric")


def _plot(frame: pd.DataFrame, output: Path) -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    shown = frame.loc[["annual_return", "max_drawdown", "volatility"], ["before", "after"]]
    shown.index = ["年化收益", "最大回撤", "年化波动率"]
    ax = shown.plot(kind="bar", figsize=(10, 6), color=["#7f8c8d", "#1976d2"])
    ax.set_title("红利低波策略根因修复前后对比")
    ax.set_ylabel("比例")
    ax.yaxis.set_major_formatter(lambda value, _position: f"{value:.0%}")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(["修复前", "修复后"])
    ax.figure.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    ax.figure.savefig(output, dpi=180)
    plt.close(ax.figure)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对比根因修复前后的消融结果")
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument(
        "--output", default="output/ablation/ablation_comparison.png"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    frame = compare(_load(args.before), _load(args.after))
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    _plot(frame, output)
    print(frame.to_string(float_format=lambda value: f"{value:.4f}"))
    print(f"对比图：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
