#!/usr/bin/env python
"""Mark omitted high-correlation pairs in an orthogonality report."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Iterable

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "output" / "orthogonality" / "orthogonality_report.md"
DEFAULT_OUTPUT = ROOT / "output" / "orthogonality" / "orthogonality_report_fixed.md"


def parse_conflict(value: str) -> tuple[str, str, float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 3 or not parts[0] or not parts[1]:
        raise argparse.ArgumentTypeError("冲突对必须为 factor_a,factor_b,correlation")
    try:
        correlation = float(parts[2])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("冲突相关系数必须为数字") from exc
    if not -1.0 <= correlation <= 1.0:
        raise argparse.ArgumentTypeError("相关系数必须位于 [-1, 1]")
    return parts[0], parts[1], correlation


def conflicts_from_matrix(path: Path, threshold: float) -> list[tuple[str, str, float]]:
    frame = pd.read_csv(path, index_col=0)
    pairs: list[tuple[str, str, float]] = []
    for index, left in enumerate(frame.index):
        for right in frame.columns[index + 1 :]:
            value = float(frame.loc[left, right])
            if abs(value) >= threshold:
                pairs.append((str(left), str(right), value))
    return pairs


def update_report_text(
    text: str,
    conflicts: Iterable[tuple[str, str, float]],
    *,
    threshold: float,
) -> str:
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("corr-threshold 必须位于 [0, 1]")
    unique: list[tuple[str, str, float]] = []
    seen: set[frozenset[str]] = set()
    for left, right, correlation in conflicts:
        key = frozenset((left, right))
        if key in seen:
            continue
        if abs(float(correlation)) < threshold:
            raise ValueError(
                f"{left}/{right} 的 |r|={abs(correlation):.4f} 低于阈值 {threshold:.2f}"
            )
        unique.append((left, right, float(correlation)))
        seen.add(key)
    if not unique:
        raise ValueError("没有达到阈值的冲突对可标记")

    text = text.replace(
        "- 最终因子：",
        "- 正交性阶段候选因子（非主配置裁决）：",
    ).replace(
        "诊断通过口径：IC均值≥0.010、IC_IR≥0.15、纯化IC均值≥0.010。",
        "辅助诊断参考口径：IC均值≥0.010、IC_IR≥0.15、纯化IC均值≥0.010；不作为主配置硬闸门。主配置唯一准入闸门为开发期纯化 IC Newey-West t≥2。",
    ).replace(
        "- 最终池限制为 6 个，",
        "- 正交性候选池限制为 6 个，",
    )
    lines = text.splitlines()
    try:
        heading = lines.index("## 高相关冲突")
    except ValueError:
        heading = len(lines)
        lines.extend(["", "## 高相关冲突", ""])
    next_heading = next(
        (index for index in range(heading + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    section = lines[heading + 1 : next_heading]
    section = [
        line for line in section
        if not re.match(r"^- 无 \|r\|\s*[>≥]", line.strip())
        and not line.strip().startswith("判定阈值：")
    ]
    while section and not section[0].strip():
        section.pop(0)
    while section and not section[-1].strip():
        section.pop()
    markers = []
    for left, right, correlation in unique:
        marker = (
            f"- ⚠️ 高相关冲突对（竞争上岗已裁决）：`{left}` / `{right}`，"
            f"r={correlation:.4f}。"
        )
        if not any(left in line and right in line and "高相关冲突对" in line for line in section):
            markers.append(marker)
    replacement = [
        "",
        f"判定阈值：**|r| ≥ {threshold:.2f}**。",
        "",
        *markers,
        *( [""] if markers and section else [] ),
        *section,
        "",
    ]
    updated = lines[: heading + 1] + replacement + lines[next_heading:]
    return "\n".join(updated).rstrip() + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="修正因子正交性报告的冲突标记")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--corr-threshold", type=float, default=0.70)
    parser.add_argument("--mark-conflict", type=parse_conflict, action="append")
    parser.add_argument("--fix", action="store_true", help="兼容命令；从相关矩阵自动识别")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = args.input if args.input.is_absolute() else ROOT / args.input
    output_path = args.output if args.output.is_absolute() else ROOT / args.output
    conflicts = list(args.mark_conflict or ())
    if not conflicts:
        matrix = input_path.with_name("correlation_matrix.csv")
        conflicts = conflicts_from_matrix(matrix, args.corr_threshold)
    updated = update_report_text(
        input_path.read_text(encoding="utf-8"),
        conflicts,
        threshold=args.corr_threshold,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(updated, encoding="utf-8")
    print(f"已标记 {len(conflicts)} 个高相关冲突对：{output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
