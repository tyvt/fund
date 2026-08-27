#!/usr/bin/env python
"""Assemble the four-layer robustness report and enforce promotion gating."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output/robustness"


def _json(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def _pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return "—" if not math.isfinite(number) else f"{number:.2%}"


def _num(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return "—" if not math.isfinite(number) else f"{number:.{digits}f}"


def _table(frame: pd.DataFrame) -> str:
    columns = [str(column) for column in frame.columns]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in frame.itertuples(index=False, name=None):
        lines.append("| " + " | ".join(str(value).replace("|", "\\|") for value in row) + " |")
    return "\n".join(lines)


def _formal_fusion() -> dict[str, Any] | None:
    return _json(ROOT / "output/ablation/metrics_fusion_optimal.json")


def _promotion_matrix(
    rollback: dict[str, Any],
    candidate: dict[str, Any],
    monte_carlo: dict[str, Any],
    fusion_scan: dict[str, Any],
    fusion: dict[str, Any] | None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    yearly = rollback.get("yearly_acceptance", {})
    layer1 = bool(
        yearly.get("year_count") == 10
        and yearly.get("annual_return_std_pass", False)
        and yearly.get("single_year_contribution_pass", False)
        and yearly.get("drawdown_year_match", False)
    )
    acceptance = monte_carlo.get("acceptance", {})
    rolling_pass = bool(
        acceptance.get("rolling_mean_gt_10pct", False)
        and acceptance.get("rolling_std_lt_5pct", False)
        and acceptance.get("all_rolling_windows_gt_10pct", False)
    )
    fusion_full = (fusion or {}).get("metrics", {}).get("full", {})
    fusion_holdings = next(
        (
            row for row in (fusion or {}).get("holdings_summary", [])
            if row.get("strategy") == "full"
        ),
        {},
    )
    max_holding = (fusion or {}).get("full_execution", {}).get("max_holding")
    fusion_pass = bool(
        fusion
        and float(fusion_holdings.get("average_eligible", 0.0)) > 30.0
        and float(fusion_full.get("annual_return", -1.0)) >= 0.16
        and float(fusion_full.get("sharpe_ratio", -1.0)) >= 0.65
        and float(fusion_full.get("turnover", 1.0)) <= 0.30
        and int(max_holding or 999) <= 13
        and fusion_scan.get("candidate_100_plateau_pass", False)
    )
    rows = [
        ("1", "年度稳定性", "10年完整、收益标准差<10%、单年贡献≤50%、最差回撤年匹配", layer1),
        ("2", "候选池枯竭", "候选≤9的月份<20%", bool(candidate.get("depletion_pass", False))),
        ("3", "缓冲依赖", "buffer_contribution占比≤30%", bool(candidate.get("buffer_dependency_pass", False))),
        ("4", "随机剔除", "20%持仓剔除模拟5%分位>10%", bool(acceptance.get("random_drop_p05_gt_10pct", False))),
        ("5", "调仓时点", "±13交易日偏移模拟5%分位>10%", bool(acceptance.get("shifted_rebalance_p05_gt_10pct", False))),
        ("6", "滚动窗口", "均值>10%、标准差<5%、所有窗口>10%", rolling_pass),
        ("7", "融合排序", "候选/收益/夏普/换手/持仓上限及100只平原区全部达标", fusion_pass),
    ]
    frame = pd.DataFrame(rows, columns=["序号", "验收项", "门槛", "passed"])
    frame["状态"] = frame["passed"].map(lambda value: "通过" if value else "未通过")
    passed = int(frame["passed"].sum())
    decision = {
        "passed": passed,
        "total": 7,
        "promotion_threshold": 5,
        "promote_to_production": passed >= 5,
        "fusion_composite_pass": fusion_pass,
    }
    return frame.drop(columns="passed"), decision


def _promote_config(fusion: dict[str, Any], decision: dict[str, Any]) -> bool:
    if not decision["promote_to_production"]:
        return False
    path = ROOT / "config/vectorbt/strategy_params.yaml"
    text = path.read_text(encoding="utf-8")
    params = fusion.get("experiment_params", {})
    replacements = {
        r"(?m)^(\s*fusion_status:)\s*experimental\s*$": r"\1 production",
        r"(?m)^(\s*fusion_mode:)\s*false\s*$": r"\1 true",
        r"(?m)^(\s*fusion_candidate_n:)\s*\d+\s*$": rf"\1 {int(params.get('fusion_candidate_n', 100))}",
        r"(?m)^(\s*dividend_weight:)\s*[0-9.]+\s*$": rf"\1 {float(params.get('dividend_weight', 0.5)):.2f}",
        r"(?m)^(\s*volatility_weight:)\s*[0-9.]+\s*$": rf"\1 {float(params.get('volatility_weight', 0.5)):.2f}",
    }
    updated = text
    for pattern, replacement in replacements.items():
        updated = re.sub(pattern, replacement, updated, count=1)
    if updated != text:
        path.write_text(updated, encoding="utf-8")
        return True
    return False


def generate(*, promote: bool = True) -> tuple[Path, dict[str, Any]]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    rollback = _json(ROOT / "output/ablation/metrics_rollback_top_buffer.json", {})
    candidate = _json(OUTPUT / "candidate_pool_summary.json", {})
    monte_carlo = _json(OUTPUT / "monte_carlo_summary.json", {})
    fusion_scan = _json(OUTPUT / "fusion_scan_summary.json", {})
    fusion = _formal_fusion()
    matrix, decision = _promotion_matrix(
        rollback, candidate, monte_carlo, fusion_scan, fusion
    )
    config_promoted = bool(promote and fusion and _promote_config(fusion, decision))
    decision["config_promoted"] = config_promoted
    decision["config_status"] = "production" if decision["promote_to_production"] else "experimental"
    (OUTPUT / "promotion_status.json").write_text(
        json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    yearly = pd.DataFrame(rollback.get("yearly_results", []))
    if not yearly.empty:
        yearly_display = yearly.copy()
        yearly_display["annual_return"] = yearly_display["annual_return"].map(_pct)
        yearly_display["max_drawdown"] = yearly_display["max_drawdown"].map(_pct)
        yearly_display["sharpe"] = yearly_display["sharpe"].map(_num)
        yearly_display["turnover"] = yearly_display["turnover"].map(_pct)
        yearly_display["avg_candidates"] = yearly_display["avg_candidates"].map(_num)
        yearly_display["beat_baseline0"] = yearly_display["beat_baseline0"].map(lambda value: "是" if value else "否")
        yearly_display = yearly_display[
            ["year", "annual_return", "max_drawdown", "sharpe", "turnover", "avg_candidates", "beat_baseline0"]
        ]
        yearly_display.columns = ["年份", "年化收益", "最大回撤", "夏普", "换手率", "平均候选池", "跑赢Baseline 0?"]
        yearly_table = _table(yearly_display)
    else:
        yearly_table = "尚未生成分年度结果。"

    fusion_full = (fusion or {}).get("metrics", {}).get("full", {})
    fusion_holding = next((row for row in (fusion or {}).get("holdings_summary", []) if row.get("strategy") == "full"), {})
    random_summary = monte_carlo.get("random_drop", {})
    shift_summary = monte_carlo.get("shifted_rebalance", {})
    block_summary = monte_carlo.get("block_bootstrap", {})
    rolling = monte_carlo.get("rolling_window", {})
    best = fusion_scan.get("recommended_candidate_100", fusion_scan.get("best_by_sharpe", {}))
    lines = [
        "# 稳健性验证与融合排序综合报告",
        "",
        "## 1. 分年度回测",
        "",
        yearly_table,
        "",
        f"年度收益标准差：{_pct(rollback.get('yearly_acceptance', {}).get('annual_return_std'))}。",
        "",
        "## 2. 候选池归因",
        "",
        f"- 平均最终候选池：{_num(candidate.get('average_final_candidates'))} 只",
        f"- 候选枯竭频率：{_pct(candidate.get('depletion_frequency'))}",
        f"- core_contribution：{_pct(candidate.get('core_annual_return_contribution'))}",
        f"- buffer_contribution：{_pct(candidate.get('buffer_annual_return_contribution'))}",
        f"- buffer贡献占比：{_pct(candidate.get('buffer_contribution_ratio'))}",
        "",
        "![候选池分析](candidate_pool_analysis.png)",
        "",
        "## 3. 压力测试",
        "",
        "| 测试 | 均值年化 | 标准差 | 5%分位 | 95%分位 |",
        "|---|---:|---:|---:|---:|",
        f"| 随机剔除20%持仓 | {_pct(random_summary.get('mean'))} | {_pct(random_summary.get('std'))} | {_pct(random_summary.get('p05'))} | {_pct(random_summary.get('p95'))} |",
        f"| 调仓日偏移±13交易日 | {_pct(shift_summary.get('mean'))} | {_pct(shift_summary.get('std'))} | {_pct(shift_summary.get('p05'))} | {_pct(shift_summary.get('p95'))} |",
        f"| 3个月Block Bootstrap | {_pct(block_summary.get('mean'))} | {_pct(block_summary.get('std'))} | {_pct(block_summary.get('p05'))} | {_pct(block_summary.get('p95'))} |",
        "",
        f"滚动3年窗口：均值 {_pct(rolling.get('mean'))}，标准差 {_pct(rolling.get('std'))}，最小值 {_pct(rolling.get('min'))}。",
        "调仓日偏移测试复用原点时选股信号，仅移动执行日，并对相邻月份保持严格时间顺序；"
        "Block Bootstrap 按3个月连续块重采样调仓周期收益。",
        "",
        "![蒙特卡洛分布](monte_carlo_distribution.png)",
        "",
        "## 4. 融合排序结果",
        "",
        f"扫描最优夏普参数：dividend_weight={_num(best.get('dividend_weight'))}，"
        f"volatility_weight={_num(best.get('volatility_weight'))}，candidate_n={best.get('fusion_candidate_n', '—')}。",
        f"100只候选平原区检验：{'通过' if fusion_scan.get('candidate_100_plateau_pass') else '未通过'}。",
        "",
        f"正式融合回测：年化 {_pct(fusion_full.get('annual_return'))}，夏普 {_num(fusion_full.get('sharpe_ratio'))}，"
        f"最大回撤 {_pct(fusion_full.get('max_drawdown'))}，换手 {_pct(fusion_full.get('turnover'))}，"
        f"平均候选池 {_num(fusion_holding.get('average_eligible'))}。",
        "",
        "![融合扫描热力图](fusion_scan_heatmap.png)",
        "",
        "## 5. 综合验收与配置决策",
        "",
        _table(matrix),
        "",
        f"通过 **{decision['passed']}/7** 项；升级门槛为至少 5 项。",
        f"配置结论：**{decision['config_status']}**。"
        + (" 已将融合模式升级为生产默认。" if config_promoted else " 未改动生产默认配置。"),
        "",
        "## 6. 结论",
        "",
        (
            "综合稳健性达到升级门槛，融合排序可进入生产默认；仍需持续监控未通过项目。"
            if decision["promote_to_production"]
            else "综合稳健性尚未达到升级门槛，融合排序继续保持 experimental。"
        ),
        "",
    ]
    report = OUTPUT / "robustness_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report, decision


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成稳健性综合报告")
    parser.add_argument("--no-promote", action="store_true", help="即使达标也不修改生产配置")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report, decision = generate(promote=not args.no_promote)
    print(json.dumps(decision, ensure_ascii=False, indent=2))
    print(f"综合报告：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
