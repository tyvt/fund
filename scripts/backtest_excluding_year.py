#!/usr/bin/env python
"""Recalculate all strategy metrics after excluding the requested year."""

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

from scripts.audit_period_metrics import calculate_period_metrics, mean_turnover_for_years


OUTPUT = ROOT / "output" / "excluding_2015"


def run_excluding_year(tag: str, exclude: int) -> dict[str, object]:
    safe_tag = re.sub(r"[^0-9A-Za-z_-]+", "_", str(tag)).strip("_")
    metrics_path = ROOT / "output" / "ablation" / f"metrics_{safe_tag}.json"
    nav_path = ROOT / "output" / "ablation" / f"nav_series_{safe_tag}.csv"
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    nav = pd.read_csv(nav_path, index_col=0, parse_dates=True)["full"].astype(float)
    years = sorted(set(int(year) for year in nav.index.year))
    if exclude not in years:
        raise ValueError(f"净值区间不包含 {exclude} 年")
    if exclude != years[0]:
        raise ValueError("为避免拼接不连续路径，本审计脚本只允许剔除区间首年")
    retained = nav.loc[nav.index.year != exclude]
    full = calculate_period_metrics(nav)
    without = calculate_period_metrics(retained)
    turnover = payload["full_execution"].get("turnover_by_date", {})
    full["turnover"] = mean_turnover_for_years(turnover, years)
    without["turnover"] = mean_turnover_for_years(turnover, [year for year in years if year != exclude])
    result: dict[str, object] = {
        "tag": safe_tag,
        "excluded_year": int(exclude),
        "full_period": full,
        "without_excluded_year": without,
        "annual_return_difference": float(without["annual_return"] - full["annual_return"]),
        "purpose": "验证全区间年化收益是否过度依赖 2015 年单年贡献",
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "metrics_without_2015.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# fusion_v2 剔除 2015 年后的全指标表",
        "",
        "| 口径 | 年化收益 | 夏普 | 最大回撤 | 换手率 |",
        "|---|---:|---:|---:|---:|",
        f"| 全区间（{years[0]}-{years[-1]}） | {full['annual_return']:.2%} | {full['sharpe_ratio']:.2f} | {full['max_drawdown']:.2%} | {full['turnover']:.2%} |",
        f"| 剔除 {exclude} 年（{exclude + 1}-{years[-1]}） | {without['annual_return']:.2%} | {without['sharpe_ratio']:.2f} | {without['max_drawdown']:.2%} | {without['turnover']:.2%} |",
        "",
        f"剔除后的年化收益变化：**{result['annual_return_difference']:+.2%}**。",
        "",
    ]
    (OUTPUT / "metrics_without_2015.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="剔除首年后的 fusion_v2 全指标")
    parser.add_argument("--tag", default="fusion_v2_fixed")
    parser.add_argument("--exclude", type=int, default=2015)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_excluding_year(args.tag, args.exclude)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
