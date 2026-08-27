#!/usr/bin/env python
"""Analyze Full-strategy turnover before and after the rebalance buffer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib import font_manager


ROOT = Path(__file__).resolve().parents[1]


def _resolve(path: str | Path) -> Path:
    value = Path(path)
    return value if value.is_absolute() else ROOT / value


def _load(path: str | Path) -> dict[str, Any]:
    target = _resolve(path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    if "full" not in payload.get("metrics", {}):
        raise ValueError(f"结果缺少 metrics.full：{target}")
    return payload


def analyze(
    result_path: str | Path,
    *,
    before_path: str | Path = "output/ablation/metrics_after_fix.json",
    output_path: str | Path = "output/ablation/turnover_comparison.png",
) -> dict[str, float | int | bool]:
    before = _load(before_path)
    after = _load(result_path)
    old = float(before["metrics"]["full"]["turnover"])
    new = float(after["metrics"]["full"]["turnover"])
    max_sells = int(after.get("full_execution", {}).get("max_sells_per_rebalance", 0))
    summary: dict[str, float | int | bool] = {
        "before_turnover": old,
        "after_turnover": new,
        "change": new - old,
        "reduction": old - new,
        "meets_25pct_target": new <= 0.25,
        "max_sells_per_rebalance": max_sells,
        "meets_sell_cap": max_sells <= 3,
    }

    for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")):
        if path.is_file():
            font_manager.fontManager.addfont(str(path))
            plt.rcParams["font.sans-serif"] = [
                font_manager.FontProperties(fname=str(path)).get_name()
            ]
            break
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(8, 5))
    bars = ax.bar(["修复前", "Band + 缓冲"], [old, new], color=["#7f8c8d", "#1976d2"])
    ax.axhline(0.25, color="#c62828", linestyle="--", linewidth=1.2, label="验收线 25%")
    ax.set_title("Full 策略年均单边换手率对比")
    ax.set_ylabel("换手率")
    ax.yaxis.set_major_formatter(lambda value, _position: f"{value:.0%}")
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    for bar, value in zip(bars, (old, new)):
        ax.text(bar.get_x() + bar.get_width() / 2, value, f"{value:.2%}", ha="center", va="bottom")
    fig.tight_layout()
    target = _resolve(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target, dpi=180)
    plt.close(fig)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分析调仓缓冲前后的换手率")
    parser.add_argument("--result", required=True, help="修复后 metrics JSON")
    parser.add_argument("--before", default="output/ablation/metrics_after_fix.json")
    parser.add_argument("--output", default="output/ablation/turnover_comparison.png")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = analyze(args.result, before_path=args.before, output_path=args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"换手率对比图：{_resolve(args.output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
