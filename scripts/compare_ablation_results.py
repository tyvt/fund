#!/usr/bin/env python
"""Compare original Top, Band + Buffer, and rollback Top + Buffer results."""

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
    for key in (
        "annual_return",
        "max_drawdown",
        "sharpe_ratio",
        "volatility",
        "turnover",
    ):
        old = float(before_full[key])
        new = float(after_full[key])
        rows.append({"metric": key, "before": old, "after": new, "change": new - old})
    return pd.DataFrame(rows).set_index("metric")


def compare_three(
    before: dict[str, Any], band: dict[str, Any], after: dict[str, Any]
) -> pd.DataFrame:
    """Return the common Full metrics for the requested three stages."""
    stages = {
        "original_top": before["metrics"]["full"],
        "band_buffer": band["metrics"]["full"],
        "top_buffer": after["metrics"]["full"],
    }
    keys = ("annual_return", "max_drawdown", "sharpe_ratio", "volatility", "turnover")
    return pd.DataFrame(
        {
            stage: {key: float(values[key]) for key in keys}
            for stage, values in stages.items()
        }
    )


def _plot(frame: pd.DataFrame, output: Path) -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    labels = {
        "original_top": "原 Top",
        "band_buffer": "Band + Buffer",
        "top_buffer": "Top + Buffer",
    }
    colors = ["#7f8c8d", "#e67e22", "#1976d2"]
    fig, axes = plt.subplots(1, 2, figsize=(13, 6), gridspec_kw={"width_ratios": [2.2, 1]})
    ratios = frame.loc[["annual_return", "max_drawdown", "turnover"]].copy()
    ratios.index = ["年化收益", "最大回撤", "年均单边换手"]
    ratios.rename(columns=labels).plot(kind="bar", ax=axes[0], color=colors)
    axes[0].set_title("收益、回撤与换手")
    axes[0].set_ylabel("比例")
    axes[0].yaxis.set_major_formatter(lambda value, _position: f"{value:.0%}")
    axes[0].grid(axis="y", alpha=0.25)
    sharpe = frame.loc[["sharpe_ratio"]].copy()
    sharpe.index = ["夏普比率"]
    sharpe.rename(columns=labels).plot(kind="bar", ax=axes[1], color=colors, legend=False)
    axes[1].set_title("风险调整后收益")
    axes[1].grid(axis="y", alpha=0.25)
    fig.suptitle("红利低波策略三阶段对比")
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180)
    plt.close(fig)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对比低波策略三个阶段的消融结果")
    parser.add_argument("--before", default="output/ablation/metrics_after_fix.json")
    parser.add_argument("--band", default="output/ablation/metrics_lowvol_band_buffer.json")
    parser.add_argument("--after", default="output/ablation/metrics_rollback_top_buffer.json")
    parser.add_argument(
        "--output", default="output/ablation/three_phase_comparison.png"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    frame = compare_three(_load(args.before), _load(args.band), _load(args.after))
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    _plot(frame, output)
    print(frame.to_string(float_format=lambda value: f"{value:.4f}"))
    print(f"对比图：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
