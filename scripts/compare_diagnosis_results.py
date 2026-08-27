#!/usr/bin/env python
"""Combine quantile, single-factor and cost diagnostics into one decision report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.factor_quantile_diagnosis import diagnose_concentration, summarize_quantiles


def _resolve(path: str | Path) -> Path:
    value = Path(path)
    return value if value.is_absolute() else ROOT / value


def _pct(value: float) -> str:
    return f"{float(value):.2%}"


def compare(
    quantile_dir: str | Path,
    ablation_dir: str | Path,
    cost_dir: str | Path,
    *,
    output_path: str | Path = ROOT / "output" / "diagnosis_summary.md",
) -> str:
    quantile_root = _resolve(quantile_dir)
    ablation_root = _resolve(ablation_dir)
    cost_root = _resolve(cost_dir)
    factor_results = {}
    for factor in ("dividend_yield", "volatility_60d"):
        returns = pd.read_csv(
            quantile_root / f"{factor}_quantile_returns.csv", parse_dates=["date"]
        )
        _, diagnostics = summarize_quantiles(returns, 5)
        factor_results[factor] = {
            **diagnostics,
            "conclusion": diagnose_concentration(factor, diagnostics),
        }

    single = json.loads(
        (ablation_root / "metrics_dividend_yield_only.json").read_text(encoding="utf-8")
    )
    cost = json.loads(
        (cost_root / "metrics_cost_ablation.json").read_text(encoding="utf-8")
    )
    dividend = factor_results["dividend_yield"]
    top30 = single["metrics"]["dividend_yield_only"]["annual_return"]
    baseline1 = single["metrics"]["baseline1"]["annual_return"]
    full_current = cost["metrics"]["full_high_cost"]["annual_return"]
    erosion = cost["annual_return_erosion_vs_no_cost"]["full_high_cost"]

    if not dividend["meaningful_difference"]:
        factor_decision = "股息率分位差异不明显，按决策树应放弃或重新设计该因子。"
    elif dividend["best_quantile"] == "Q5":
        factor_decision = "股息率 Alpha 集中在 Q5，可保留 Top10 极端分位策略。"
    elif dividend["best_quantile"] in {"Q3", "Q4"}:
        factor_decision = "股息率 Alpha 位于中间分位，应优先采用 Top30 而非 Top10。"
    else:
        factor_decision = "股息率最佳分位与预期方向相反，需要重新设计选股方向。"

    top30_effective = bool(single.get("top30_effective", single["effective"]))
    if dividend["best_quantile"] == "Q5" and dividend["meaningful_difference"]:
        single_decision = (
            "作为辅助验证，Top30 未超过 Baseline 1，说明扩容会稀释 Q5 Alpha；"
            "该结果支持保留 Top10，不能用于否定极端分位。"
            if not top30_effective
            else "作为辅助验证，Top30 也超过 Baseline 1，股息率信号对扩容具有稳健性。"
        )
    else:
        single_decision = (
            "Top30 超过 Baseline 1，单因子扩容方案有效。"
            if top30_effective
            else "Top30 未超过 Baseline 1，单因子扩容方案无效。"
        )
    cost_decision = (
        "成本侵蚀超过 1.5 个百分点，应优化执行成本或降低换手。"
        if bool(cost["cost_sensitive"])
        else "成本侵蚀未超过 1.5 个百分点，主要问题在因子组合本身。"
    )
    no_cost_full = cost["metrics"]["full_no_cost"]["annual_return"]
    structural_gap = baseline1 - no_cost_full
    combined_decision = (
        f"即使完全免成本，Full 年化仍比 Baseline 1 低 {structural_gap:.2%}；"
        "因此成本敏感与因子/约束组合问题同时存在。"
        if structural_gap > 0
        else "免成本 Full 已超过 Baseline 1，收益拖累主要可由交易成本解释。"
    )
    lines = [
        "# 因子分位与单因子回测综合诊断",
        "",
        "## 决策结果",
        "",
        f"1. {factor_decision}",
        f"2. {single_decision}",
        f"3. {cost_decision}",
        f"4. {combined_decision}",
        "",
        "## 关键证据",
        "",
        "| 项目 | 结果 |",
        "|---|---:|",
        f"| dividend_yield 最佳分位 | {dividend['best_quantile']} |",
        f"| dividend_yield 最佳-最差年化差 | {_pct(dividend['best_minus_worst_annual_return'])} |",
        f"| volatility_60d 最佳分位 | {factor_results['volatility_60d']['best_quantile']} |",
        f"| dividend_yield Top30 年化 | {_pct(top30)} |",
        f"| Baseline 1 年化 | {_pct(baseline1)} |",
        f"| 当前成本 Full 年化 | {_pct(full_current)} |",
        f"| 无成本 Full 年化 | {_pct(no_cost_full)} |",
        f"| 当前成本年化收益侵蚀 | {_pct(erosion)} |",
        "",
        "## 分项结论",
        "",
        f"- dividend_yield：{factor_results['dividend_yield']['conclusion']}",
        f"- volatility_60d：{factor_results['volatility_60d']['conclusion']}",
        f"- 单因子：{single_decision}",
        f"- 成本：{cost['conclusion']}",
        "",
    ]
    report = "\n".join(lines)
    target = _resolve(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report, encoding="utf-8")
    print(report)
    print(f"综合报告：{target}")
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="汇总三步诊断结果")
    parser.add_argument("--quantile", default="output/quantile")
    parser.add_argument("--ablation", default="output/ablation")
    parser.add_argument("--cost", default="output/cost")
    parser.add_argument("--output", default="output/diagnosis_summary.md")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from market_data import configure_stdout_utf8

    configure_stdout_utf8()
    args = parse_args(argv)
    compare(args.quantile, args.ablation, args.cost, output_path=args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
