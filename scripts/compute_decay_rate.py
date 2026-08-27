#!/usr/bin/env python
"""Report development-period versus locked-period strategy decay."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_period_metrics import calculate_period_metrics
from scripts.compute_factor_t_stats import parse_period


OUTPUT = ROOT / "output" / "decay"


def run_decay(
    tag: str,
    train: tuple,
    test: tuple,
) -> dict[str, object]:
    safe_tag = re.sub(r"[^0-9A-Za-z_-]+", "_", str(tag)).strip("_")
    nav_path = ROOT / "output" / "ablation" / f"nav_series_{safe_tag}.csv"
    nav = pd.read_csv(nav_path, index_col=0, parse_dates=True)["full"].astype(float)
    train_start, train_end = train
    test_start, test_end = test
    development = calculate_period_metrics(
        nav.loc[(nav.index.date >= train_start) & (nav.index.date <= train_end)]
    )
    locked = calculate_period_metrics(
        nav.loc[(nav.index.date >= test_start) & (nav.index.date <= test_end)]
    )
    base = float(development["annual_return"])
    if base == 0.0:
        raise ZeroDivisionError("开发期年化收益为 0，无法计算衰减率")
    decay_rate = (float(locked["annual_return"]) - base) / base
    result: dict[str, object] = {
        "tag": safe_tag,
        "formula": "(locked annual return - development annual return) / development annual return",
        "development": development,
        "locked": locked,
        "decay_rate": float(decay_rate),
        "threshold": -0.30,
        "passed": bool(decay_rate > -0.30),
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "decay_rate_results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    status = "PASS" if result["passed"] else "FAIL"
    lines = [
        "# fusion_v2 锁定期衰减率",
        "",
        "| 口径 | 年化收益 | 夏普 | 最大回撤 |",
        "|---|---:|---:|---:|",
        f"| 开发期（{train_start.year}-{train_end.year}） | {development['annual_return']:.2%} | {development['sharpe_ratio']:.2f} | {development['max_drawdown']:.2%} |",
        f"| 锁定期（{test_start.year}-{test_end.year}） | {locked['annual_return']:.2%} | {locked['sharpe_ratio']:.2f} | {locked['max_drawdown']:.2%} |",
        "",
        f"年化收益衰减率：**{decay_rate:.2%}**；验收线 `> -30%`；状态：**{status}**。",
        "",
    ]
    (OUTPUT / "decay_rate_table.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="fusion_v2 开发期/锁定期衰减率")
    parser.add_argument("--tag", default="fusion_v2_fixed")
    parser.add_argument("--train", type=parse_period, default=parse_period("2015-2019"))
    parser.add_argument("--test", type=parse_period, default=parse_period("2020-2024"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_decay(args.tag, args.train, args.test)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
