#!/usr/bin/env python
"""Run the four-way point-in-time constraint ablation experiment."""

from __future__ import annotations

import argparse
import gc
import html
import json
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vbt.adapters import DEFAULT_FACTORS, VBTDataLoader
from vbt.config import load_yaml, reproducibility_snapshot
from vbt.engine import PerformanceCalculator, VBTEngine
from vbt.strategies import AblationStrategy
from vbt.strategies.ablation import STRATEGY_NAMES


LABELS = {
    "baseline0": "Baseline 0（市场）",
    "baseline1": "Baseline 1（硬约束）",
    "baseline2": "Baseline 2（股息率）",
    "full": "Full（完整策略）",
    "dividend_yield_only": "股息率 Top30（仅硬约束）",
}
ATTRIBUTION_LABELS = {
    "hard_constraints": "硬约束贡献",
    "factor_selection": "因子选股贡献",
    "full_constraints": "完整约束贡献",
}


def _configure_chinese_font() -> None:
    for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")):
        if path.is_file():
            font_manager.fontManager.addfont(str(path))
            plt.rcParams["font.sans-serif"] = [font_manager.FontProperties(fname=str(path)).get_name()]
            break
    plt.rcParams["axes.unicode_minus"] = False


def load_config(path: str | Path) -> dict[str, Any]:
    config = load_yaml(path)
    required = {"backtest", "selection", "hard_constraints", "full_constraints", "output"}
    missing = required - set(config)
    if missing:
        raise ValueError(f"消融配置缺少分组：{', '.join(sorted(missing))}")
    return config


def strategy_params(config: dict[str, Any]) -> dict[str, Any]:
    return {
        **config["selection"],
        **config["hard_constraints"],
        **config["full_constraints"],
        "rebalance_freq": config["backtest"]["rebalance_freq"],
        "rebalance_month": config["backtest"].get("rebalance_month", 1),
        "rebalance_day": config["backtest"].get("rebalance_day", -1),
        "holding_period": config["backtest"].get("holding_period", 20),
        "alignment_mode": False,
    }


def fusion_defaults(path: str | Path = "config/vectorbt/strategy_params.yaml") -> dict[str, Any]:
    """Load the audit-locked fusion defaults from the strategy config."""

    raw = load_yaml(path)
    fusion = raw.get("fusion")
    if not isinstance(fusion, dict):
        raise ValueError("策略配置缺少 fusion 分组")
    factors = list(dict.fromkeys(str(value) for value in fusion.get("factors", ())))
    candidate_factors = list(
        dict.fromkeys(str(value) for value in fusion.get("candidate_factors", factors))
    )
    if not factors:
        raise ValueError("fusion.factors 不得为空")
    directions = {str(key): int(value) for key, value in dict(fusion.get("directions") or {}).items()}
    weights = {str(key): float(value) for key, value in dict(fusion.get("weights") or {}).items()}
    missing_directions = set(candidate_factors) - set(directions)
    missing_weights = set(factors) - set(weights)
    if missing_directions or missing_weights:
        raise ValueError(
            "fusion 配置不完整："
            f"缺少方向 {sorted(missing_directions)}，缺少权重 {sorted(missing_weights)}"
        )
    return {
        "fusion_mode": True,
        "fusion_status": "validation",
        "fusion_factors": factors,
        "fusion_candidate_factors": candidate_factors,
        "fusion_directions": {factor: directions[factor] for factor in factors},
        "fusion_weights": {factor: weights[factor] for factor in factors},
        "fusion_min_valid_factors": len(factors),
        "fusion_candidate_n": int(fusion.get("candidate_n", 100)),
        "top_n": int(fusion.get("top_n", 20)),
        "min_holdings": int(fusion.get("top_n", 20)),
        "max_holding": int(fusion.get("top_n", 20)),
        "max_single_weight": float(fusion.get("max_single_weight", 0.08)),
        "hold_bonus": float(fusion.get("hold_bonus", 0.10)),
        "cost_threshold": float(fusion.get("cost_threshold", 0.01)),
        "overheat_filter_enabled": True,
        "overheat_factor": "reversal_5d",
        "overheat_quantile": 0.95,
        "rebalance_buffer": {"enabled": False},
    }


def fusion_cli_overrides(
    *,
    factors: str | None,
    equal_weight: bool,
) -> dict[str, Any]:
    """Resolve an explicit, reproducible fusion factor pool for the CLI."""

    resolved = fusion_defaults()
    selected = (
        list(dict.fromkeys(value.strip() for value in factors.split(",") if value.strip()))
        if factors is not None
        else list(resolved["fusion_factors"])
    )
    if not selected:
        raise ValueError("--factors 至少需要一个因子")
    configured = set(resolved.pop("fusion_candidate_factors", resolved["fusion_factors"]))
    unknown = set(selected) - configured
    if unknown:
        raise ValueError(f"--factors 包含未配置因子：{', '.join(sorted(unknown))}")
    resolved["fusion_factors"] = selected
    resolved["fusion_directions"] = {
        factor: resolved["fusion_directions"][factor] for factor in selected
    }
    if equal_weight:
        weight = 1.0 / len(selected)
        resolved["fusion_weights"] = {factor: weight for factor in selected}
    else:
        raw = {factor: resolved["fusion_weights"][factor] for factor in selected}
        total = sum(raw.values())
        if total <= 0.0:
            raise ValueError("入选因子权重之和必须大于 0")
        resolved["fusion_weights"] = {
            factor: value / total for factor, value in raw.items()
        }
    resolved["fusion_min_valid_factors"] = len(selected)
    return resolved


def experiment_params(config: dict[str, Any], tag: str | None) -> dict[str, Any]:
    """Return parameter overrides for a named experiment configuration group."""
    safe_tag = re.sub(r"[^0-9A-Za-z_-]+", "_", str(tag or "")).strip("_")
    experiment = config.get(safe_tag)
    if not isinstance(experiment, dict):
        return {}
    overrides: dict[str, Any] = {}
    for group in ("selection", "hard_constraints", "full_constraints"):
        value = experiment.get(group)
        if isinstance(value, dict):
            overrides.update(value)
    nested_overrides = experiment.get("overrides")
    if nested_overrides is not None and not isinstance(nested_overrides, dict):
        raise ValueError(f"实验组 {safe_tag} 的 overrides 必须是映射")
    if isinstance(nested_overrides, dict):
        overrides.update(nested_overrides)
    for key, value in experiment.items():
        if key not in {
            "name",
            "description",
            "extends",
            "selection",
            "hard_constraints",
            "full_constraints",
            "overrides",
        }:
            overrides[key] = value
    return overrides


def calculate_attribution(metrics: dict[str, dict[str, float]]) -> dict[str, Any]:
    annual = {name: float(values["annual_return"]) for name, values in metrics.items()}
    contributions = {
        "hard_constraints": annual["baseline1"] - annual["baseline0"],
        "factor_selection": annual["baseline2"] - annual["baseline1"],
        "full_constraints": annual["full"] - annual["baseline2"],
    }
    excess_return = annual["full"] - annual["baseline0"]
    closure = float(sum(contributions.values()) - excess_return)
    if math.isclose(excess_return, 0.0, abs_tol=1e-12):
        percentages = {name: float("nan") for name in contributions}
    else:
        percentages = {
            name: float(value / excess_return * 100.0)
            for name, value in contributions.items()
        }
    return {
        "basis": "full_minus_baseline0_annual_return",
        "baseline0_annual_return": annual["baseline0"],
        "full_annual_return": annual["full"],
        "excess_return": excess_return,
        "contributions": contributions,
        "percentages": percentages,
        "closure_error": closure,
    }


def _finite(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return number if np.isfinite(number) else float("nan")


def _metrics(result) -> dict[str, float]:
    values = PerformanceCalculator(result).compute_metrics()
    returns = result.nav.astype(float).pct_change().dropna()
    values["volatility"] = (
        float(returns.std(ddof=1) * math.sqrt(252.0)) if len(returns) > 1 else float("nan")
    )
    return {key: _finite(value) for key, value in values.items()}


def calculate_yearly_performance(
    navs: pd.DataFrame,
    full_metadata: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Calculate calendar-year metrics from one continuous point-in-time run."""
    daily_returns = navs.pct_change()
    stage_counts = full_metadata.get("candidate_stage_counts", {})
    final_counts = pd.Series(stage_counts.get("final", {}), dtype=float)
    if not final_counts.empty:
        final_counts.index = pd.to_datetime(final_counts.index)
    turnover = pd.Series(full_metadata.get("turnover_by_date", {}), dtype=float)
    if not turnover.empty:
        turnover.index = pd.to_datetime(turnover.index)

    rows: list[dict[str, Any]] = []
    log_contributions: dict[int, float] = {}
    for year in sorted(set(navs.index.year)):
        period_returns = daily_returns.loc[daily_returns.index.year == year]
        if period_returns.empty:
            continue
        full_returns = period_returns["full"].dropna()
        benchmark_returns = period_returns["baseline0"].dropna()
        full_total = float((1.0 + full_returns).prod() - 1.0)
        benchmark_total = float((1.0 + benchmark_returns).prod() - 1.0)
        annual_return = (
            float((1.0 + full_total) ** (252.0 / len(full_returns)) - 1.0)
            if len(full_returns)
            else float("nan")
        )
        path = (1.0 + full_returns).cumprod()
        max_drawdown = float((path / path.cummax() - 1.0).min()) if len(path) else float("nan")
        std = float(full_returns.std(ddof=1)) if len(full_returns) > 1 else float("nan")
        sharpe = (
            float(full_returns.mean() / std * math.sqrt(252.0))
            if np.isfinite(std) and std > 0.0
            else float("nan")
        )
        year_candidates = final_counts.loc[final_counts.index.year == year]
        year_turnover = turnover.loc[turnover.index.year == year]
        log_contributions[int(year)] = float(np.log1p(full_total))
        rows.append(
            {
                "year": int(year),
                "annual_return": annual_return,
                "total_return": full_total,
                "max_drawdown": max_drawdown,
                "sharpe": sharpe,
                "turnover": float(year_turnover.mean()) if len(year_turnover) else float("nan"),
                "avg_candidates": float(year_candidates.mean()) if len(year_candidates) else float("nan"),
                "benchmark_return": benchmark_total,
                "beat_baseline0": bool(full_total > benchmark_total),
            }
        )
    frame = pd.DataFrame(rows)
    total_log = float(sum(log_contributions.values()))
    if not frame.empty:
        frame["cumulative_return_contribution"] = frame["year"].map(
            lambda year: (
                log_contributions[int(year)] / total_log
                if not math.isclose(total_log, 0.0, abs_tol=1e-12)
                else float("nan")
            )
        )
    return_std = float(frame["annual_return"].std(ddof=1)) if len(frame) > 1 else float("nan")
    max_contribution = (
        float(frame["cumulative_return_contribution"].max()) if len(frame) else float("nan")
    )
    full_worst_year = (
        int(frame.loc[frame["max_drawdown"].idxmin(), "year"]) if len(frame) else None
    )
    benchmark_drawdowns: dict[int, float] = {}
    for year in frame.get("year", []):
        values = daily_returns.loc[daily_returns.index.year == int(year), "baseline0"].dropna()
        path = (1.0 + values).cumprod()
        benchmark_drawdowns[int(year)] = (
            float((path / path.cummax() - 1.0).min()) if len(path) else float("nan")
        )
    benchmark_worst_year = (
        min(benchmark_drawdowns, key=benchmark_drawdowns.get) if benchmark_drawdowns else None
    )
    acceptance = {
        "year_count": int(len(frame)),
        "annual_return_std": return_std,
        "annual_return_std_pass": bool(return_std < 0.10),
        "max_single_year_contribution": max_contribution,
        "single_year_contribution_pass": bool(max_contribution <= 0.50),
        "full_worst_drawdown_year": full_worst_year,
        "baseline0_worst_drawdown_year": benchmark_worst_year,
        "drawdown_year_match": full_worst_year == benchmark_worst_year,
    }
    return frame, acceptance


def _yearly_markdown(frame: pd.DataFrame, acceptance: dict[str, Any]) -> str:
    shown = frame.copy()
    shown["year"] = shown["year"].astype(str)
    for column in ("annual_return", "max_drawdown", "turnover", "benchmark_return"):
        shown[column] = shown[column].map(_pct)
    shown["sharpe"] = shown["sharpe"].map(_number)
    shown["avg_candidates"] = shown["avg_candidates"].map(_number)
    shown["beat_baseline0"] = shown["beat_baseline0"].map(lambda value: "是" if value else "否")
    shown = shown[
        ["year", "annual_return", "max_drawdown", "sharpe", "turnover", "avg_candidates", "beat_baseline0"]
    ].rename(
        columns={
            "year": "年份",
            "annual_return": "年化收益",
            "max_drawdown": "最大回撤",
            "sharpe": "夏普",
            "turnover": "换手率",
            "avg_candidates": "平均候选池",
            "beat_baseline0": "跑赢 Baseline 0?",
        }
    )
    return "\n".join(
        [
            "## 分年度回测",
            "",
            _markdown_table(shown),
            "",
            f"年度收益标准差：{_pct(float(acceptance['annual_return_std']))}；"
            f"最大单年累计收益贡献：{_pct(float(acceptance['max_single_year_contribution']))}；"
            f"Full/Baseline 0 最差回撤年份：{acceptance['full_worst_drawdown_year']} / "
            f"{acceptance['baseline0_worst_drawdown_year']}。",
            "",
        ]
    )


def _engine(data, strategy: AblationStrategy, backtest: dict[str, Any]) -> VBTEngine:
    stamp = float(backtest["stamp_duty"])
    return VBTEngine(
        data=data,
        strategy=strategy,
        initial_capital=float(backtest["initial_capital"]),
        commission=float(backtest["commission"]),
        min_commission=0.0,
        stamp_duty_before=stamp,
        stamp_duty_after=stamp,
        slippage=float(backtest["slippage"]),
        backtest_config={**backtest, "log_path": "logs/ablation.log"},
    )


def _artifact_name(stem: str, suffix: str, extension: str) -> str:
    return f"{stem}_{suffix}.{extension}" if suffix else f"{stem}.{extension}"


def _plot_series(navs: pd.DataFrame, output_dir: Path, suffix: str = "") -> None:
    _configure_chinese_font()
    normalized = navs.div(navs.iloc[0])
    fig, ax = plt.subplots(figsize=(12, 6))
    normalized.rename(columns=LABELS).plot(ax=ax, linewidth=1.6)
    ax.set_title("约束消融实验：四组策略净值对比")
    ax.set_xlabel("日期")
    ax.set_ylabel("净值（起点=1）")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_dir / _artifact_name("nav_comparison", suffix, "png"), dpi=160)
    plt.close(fig)

    drawdowns = navs.div(navs.cummax()).sub(1.0)
    fig, ax = plt.subplots(figsize=(12, 6))
    drawdowns.rename(columns=LABELS).plot(ax=ax, linewidth=1.4)
    ax.set_title("约束消融实验：回撤对比")
    ax.set_xlabel("日期")
    ax.set_ylabel("回撤")
    ax.yaxis.set_major_formatter(lambda value, _position: f"{value:.0%}")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_dir / _artifact_name("drawdown_comparison", suffix, "png"), dpi=160)
    plt.close(fig)


def _pct(value: float, digits: int = 2) -> str:
    return "—" if not np.isfinite(value) else f"{value:.{digits}%}"


def _number(value: float, digits: int = 2) -> str:
    return "—" if not np.isfinite(value) else f"{value:.{digits}f}"


def _markdown_table(frame: pd.DataFrame) -> str:
    """Render a compact Markdown table without pandas' optional tabulate dependency."""
    columns = [str(column) for column in frame.columns]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in frame.itertuples(index=False, name=None):
        cells = []
        for value in row:
            if isinstance(value, float):
                cells.append("—" if not np.isfinite(value) else f"{value:.4f}")
            else:
                cells.append(str(value).replace("|", "\\|"))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _holdings_display(holdings: pd.DataFrame) -> pd.DataFrame:
    shown = holdings[
        [
            "label",
            "average_holdings",
            "min_holdings",
            "max_holdings",
            "average_eligible",
            "average_invested_weight",
            "annual_one_way_turnover",
            "rebalance_count",
        ]
    ].copy()
    shown = shown.rename(
        columns={
            "label": "策略",
            "average_holdings": "平均持仓数",
            "min_holdings": "最少持仓数",
            "max_holdings": "最多持仓数",
            "average_eligible": "平均候选数",
            "average_invested_weight": "平均投资比例",
            "annual_one_way_turnover": "年均单边换手",
            "rebalance_count": "调仓次数",
        }
    )
    shown["平均投资比例"] = shown["平均投资比例"].map(_pct)
    shown["年均单边换手"] = shown["年均单边换手"].map(_pct)
    return shown


def _conclusion(
    metrics: dict[str, dict[str, float]],
    attribution: dict[str, Any],
    holdings: pd.DataFrame,
) -> str:
    full = metrics["full"]
    baseline1 = metrics["baseline1"]
    baseline2 = metrics["baseline2"]
    tolerance = 0.005
    if (
        full["annual_return"] > baseline2["annual_return"]
        and full["max_drawdown"] > baseline2["max_drawdown"]
    ):
        finding = "Full 相对 Baseline 2 提高收益并显著降低回撤，完整约束改善了风险收益结构。"
    elif abs(full["annual_return"] - baseline1["annual_return"]) <= tolerance:
        finding = "Full 与 Baseline 1 的年化收益接近，因子与完整约束的合计增益有限。"
    elif abs(full["annual_return"] - baseline2["annual_return"]) <= tolerance:
        finding = "Full 与 Baseline 2 的年化收益接近，完整约束的收益贡献有限。"
    elif full["annual_return"] < baseline2["annual_return"]:
        finding = "Full 的年化收益低于 Baseline 2，完整约束拖累了收益。"
    else:
        finding = "Full 相对 Baseline 2 存在收益与回撤之间的权衡。"
    excess = float(attribution["excess_return"])
    if math.isclose(excess, 0.0, abs_tol=1e-12):
        attribution_text = "Full 相对市场基准没有可归因的年化超额收益。"
    else:
        largest = max(
            attribution["contributions"],
            key=lambda key: abs(attribution["contributions"][key]),
        )
        attribution_text = (
            f"绝对贡献最大的组件是{ATTRIBUTION_LABELS[largest]}"
            f"（{attribution['contributions'][largest]:+.2%}）。"
        )
    full_row = holdings.loc[holdings["strategy"].eq("full")].iloc[0]
    invested = float(full_row["average_invested_weight"])
    exposure_text = (
        f"Full 平均持仓 {float(full_row['average_holdings']):.1f} 只、平均投资比例"
        f" {invested:.1%}（最少持仓 {int(full_row['min_holdings'])} 只）。"
    )
    if invested >= 0.999:
        exposure_text += "现金假象已消除，风险收益变化主要来自股票筛选和约束。"
    else:
        exposure_text += "仍存在未投资现金，需要检查候选池或约束可行性。"
    market_text = (
        f"Full 相对市场基准的年化超额收益为 {attribution['excess_return']:+.2%}。"
    )
    return f"{finding} {market_text} {attribution_text} {exposure_text}"


def _report_markdown(
    config: dict[str, Any],
    metrics: dict[str, dict[str, float]],
    attribution: dict[str, Any],
    holdings: pd.DataFrame,
) -> str:
    backtest = config["backtest"]
    lines = [
        "# 约束消融实验报告",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 实验设置",
        "",
        f"- 区间：{backtest['start_date']} ~ {backtest['end_date']}",
        f"- 初始资金：{float(backtest['initial_capital']):,.0f}",
        "- 调仓：每月最后一个交易日生成信号，下一交易日执行；持有期口径 20 个交易日",
        "- 执行：可分股目标权重；比例佣金、卖出印花税及滑点；不使用最低佣金和整手约束",
        "- 收益：前复权价格；避免除权除息被误记为投资亏损",
        "- 财务约束：ROE 剔除后 20%、负债率剔除前 20%；年报自次年 4 月 30 日起可用",
        "",
        "## 四组策略绩效",
        "",
        "| 指标 | Baseline 0 | Baseline 1 | Baseline 2 | Full |",
        "|---|---:|---:|---:|---:|",
    ]
    metric_rows = [
        ("年化收益", "annual_return", _pct),
        ("累计收益", "total_return", _pct),
        ("最大回撤", "max_drawdown", _pct),
        ("年化波动率", "volatility", _pct),
        ("夏普比率", "sharpe_ratio", _number),
        ("日胜率", "win_rate", _pct),
        ("年均单边换手率", "turnover", _pct),
    ]
    for label, key, formatter in metric_rows:
        lines.append(
            f"| {label} | "
            + " | ".join(formatter(metrics[name][key]) for name in STRATEGY_NAMES)
            + " |"
        )
    lines.extend(
        [
            "",
            "## 超额收益归因",
            "",
            "归因对象为 `Full 年化收益 − Baseline 0 年化收益`；三项贡献按该超额收益归一化。",
            "本期总超额收益为负，因此正贡献会显示为负占比、负贡献会显示为正占比；应同时阅读年化贡献的正负号。",
            "",
            "| 组件 | 年化贡献 | 超额收益占比 |",
            "|---|---:|---:|",
        ]
    )
    for key in ("hard_constraints", "factor_selection", "full_constraints"):
        contribution = attribution["contributions"][key]
        percentage = attribution["percentages"][key]
        shown_percentage = "—" if not np.isfinite(percentage) else f"{percentage:.1f}%"
        lines.append(
            f"| {ATTRIBUTION_LABELS[key]} | {contribution:+.2%} | {shown_percentage} |"
        )
    pct_total = sum(
        value for value in attribution["percentages"].values() if np.isfinite(value)
    )
    lines.extend(
        [
            f"| **合计（Full − Baseline 0）** | **{attribution['excess_return']:+.2%}** | **{pct_total:.1f}%** |",
            "",
            f"闭合误差：`{attribution['closure_error']:.3e}`。",
            "",
            "## 持仓统计",
            "",
            _markdown_table(_holdings_display(holdings)),
            "",
            "## 净值与回撤",
            "",
            "![净值对比](nav_comparison.png)",
            "",
            "![回撤对比](drawdown_comparison.png)",
            "",
            "## 结论",
            "",
            _conclusion(metrics, attribution, holdings),
            "",
        ]
    )
    return "\n".join(lines)


def _report_html(markdown_text: str, metrics: dict[str, dict[str, float]], holdings: pd.DataFrame) -> str:
    performance = pd.DataFrame(metrics).T[
        ["annual_return", "total_return", "max_drawdown", "volatility", "sharpe_ratio", "turnover"]
    ].copy()
    for column in ["annual_return", "total_return", "max_drawdown", "volatility", "turnover"]:
        performance[column] = performance[column].map(_pct)
    performance["sharpe_ratio"] = performance["sharpe_ratio"].map(_number)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>约束消融实验报告</title><style>
body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;max-width:1180px;margin:30px auto;padding:0 22px;color:#1f2937}}
h1,h2{{color:#0f3d56}}table{{border-collapse:collapse;width:100%;margin:12px 0 26px}}th,td{{padding:8px 10px;border-bottom:1px solid #dfe7eb;text-align:right}}th:first-child,td:first-child{{text-align:left}}img{{max-width:100%;height:auto}}pre{{white-space:pre-wrap;background:#f6f8fa;padding:14px;border-radius:8px}}
</style></head><body><h1>约束消融实验报告</h1>
<h2>四组策略绩效</h2>{performance.rename(index=LABELS).to_html(classes='dataframe')}
<h2>持仓统计</h2>{_holdings_display(holdings).to_html(index=False, classes='dataframe')}
<h2>净值对比</h2><img src="nav_comparison.png" alt="净值对比">
<h2>回撤对比</h2><img src="drawdown_comparison.png" alt="回撤对比">
<h2>完整 Markdown 内容</h2><pre>{html.escape(markdown_text)}</pre></body></html>"""


def _lowvol_acceptance_appendix(
    output_dir: Path,
    metrics: dict[str, dict[str, float]],
    holdings: pd.DataFrame,
    full_execution: dict[str, Any],
) -> str:
    """Build the requested before/after and acceptance section when data exists."""
    before_path = output_dir / "metrics_after_fix.json"
    if not before_path.is_file():
        return ""
    before = json.loads(before_path.read_text(encoding="utf-8"))["metrics"]["full"]
    after = metrics["full"]
    full_row = holdings.loc[holdings["strategy"].eq("full")].iloc[0]
    average_holdings = float(full_row["average_holdings"])
    invested = float(full_row["average_invested_weight"])
    max_sells = int(full_execution.get("max_sells_per_rebalance", 0))
    checks = [
        ("年化收益", after["annual_return"], ">= 16%", after["annual_return"] >= 0.16, _pct),
        ("最大回撤", after["max_drawdown"], ">= -35%（绝对回撤不超过 35%）", after["max_drawdown"] >= -0.35, _pct),
        ("夏普比率", after["sharpe_ratio"], ">= 0.70", after["sharpe_ratio"] >= 0.70, _number),
        ("年均单边换手", after["turnover"], "<= 25%", after["turnover"] <= 0.25, _pct),
        ("平均持仓数", average_holdings, "10–13", 10.0 <= average_holdings <= 13.0, _number),
        ("平均投资比例", invested, ">= 99.9%", invested >= 0.999, _pct),
        ("单期最多卖出", float(max_sells), "<= 3", max_sells <= 3, lambda value: str(int(value))),
    ]
    lines = [
        "## Band + 调仓缓冲修复前后",
        "",
        "| 指标 | 修复前 Full | 修复后 Full | 变化 |",
        "|---|---:|---:|---:|",
    ]
    for label, key, formatter in (
        ("年化收益", "annual_return", _pct),
        ("最大回撤", "max_drawdown", _pct),
        ("夏普比率", "sharpe_ratio", _number),
        ("年均单边换手", "turnover", _pct),
    ):
        old = float(before[key])
        new = float(after[key])
        lines.append(
            f"| {label} | {formatter(old)} | {formatter(new)} | {formatter(new - old)} |"
        )
    lines.extend(
        [
            "",
            "## 任务验收",
            "",
            "| 验收项 | 实测 | 门槛 | 状态 |",
            "|---|---:|---:|:---:|",
        ]
    )
    for label, value, threshold, passed, formatter in checks:
        lines.append(
            f"| {label} | {formatter(float(value))} | {threshold} | {'通过' if passed else '未通过'} |"
        )
    passed_labels = "、".join(label for label, _, _, passed, _ in checks if passed)
    failed_labels = "、".join(label for label, _, _, passed, _ in checks if not passed)
    lines.extend(
        [
            "",
            f"已通过：{passed_labels}；未通过：{failed_labels}。",
            "这些数值为当前点时数据与既定约束下的真实回测结果，未使用示例值替代。",
            "",
            "![换手率对比](turnover_comparison.png)",
            "",
        ]
    )
    return "\n".join(lines)


def _rollback_acceptance_appendix(
    output_dir: Path,
    metrics: dict[str, dict[str, float]],
    holdings: pd.DataFrame,
    full_execution: dict[str, Any],
) -> str:
    """Build the Top + Buffer three-stage comparison and acceptance table."""
    source_paths = {
        "修复前（原 Top）": output_dir / "metrics_after_fix.json",
        "Band + Buffer": output_dir / "metrics_lowvol_band_buffer.json",
    }
    if not all(path.is_file() for path in source_paths.values()):
        return ""
    stages = {
        label: json.loads(path.read_text(encoding="utf-8"))["metrics"]["full"]
        for label, path in source_paths.items()
    }
    stages["Top + Buffer"] = metrics["full"]
    after = metrics["full"]
    full_row = holdings.loc[holdings["strategy"].eq("full")].iloc[0]
    average_holdings = float(full_row["average_holdings"])
    invested = float(full_row["average_invested_weight"])
    max_sells = int(full_execution.get("max_sells_per_rebalance", 0))
    checks = [
        ("年化收益", after["annual_return"], ">= 12%", after["annual_return"] >= 0.12, _pct),
        ("最大回撤", after["max_drawdown"], ">= -36%（绝对回撤不超过 36%）", after["max_drawdown"] >= -0.36, _pct),
        ("夏普比率", after["sharpe_ratio"], ">= 0.55", after["sharpe_ratio"] >= 0.55, _number),
        ("年均单边换手", after["turnover"], "<= 30%", after["turnover"] <= 0.30, _pct),
        ("平均持仓数", average_holdings, "10–14", 10.0 <= average_holdings <= 14.0, _number),
        ("平均投资比例", invested, ">= 99.9%", invested >= 0.999, _pct),
        ("单期最多卖出", float(max_sells), "<= 3", max_sells <= 3, lambda value: str(int(value))),
    ]
    lines = [
        "## 三阶段实测对比",
        "",
        "| 指标 | 修复前（原 Top） | Band + Buffer | Top + Buffer |",
        "|---|---:|---:|---:|",
    ]
    for label, key, formatter in (
        ("年化收益", "annual_return", _pct),
        ("最大回撤", "max_drawdown", _pct),
        ("夏普比率", "sharpe_ratio", _number),
        ("年均单边换手", "turnover", _pct),
    ):
        values = [formatter(float(stage[key])) for stage in stages.values()]
        lines.append(f"| {label} | {' | '.join(values)} |")
    lines.extend(
        [
            "",
            "## 任务验收",
            "",
            "| 验收项 | 实测 | 门槛 | 状态 |",
            "|---|---:|---:|:---:|",
        ]
    )
    for label, value, threshold, passed, formatter in checks:
        lines.append(
            f"| {label} | {formatter(float(value))} | {threshold} | {'通过' if passed else '未通过'} |"
        )
    passed_labels = "、".join(label for label, _, _, passed, _ in checks if passed)
    failed_labels = "、".join(label for label, _, _, passed, _ in checks if not passed)
    lines.extend(
        [
            "",
            f"已通过：{passed_labels or '无'}；未通过：{failed_labels or '无'}。",
            "以上均为当前点时数据、交易成本和约束下的实际回测结果。",
            "",
            "![三阶段对比](three_phase_comparison.png)",
            "",
        ]
    )
    return "\n".join(lines)


def print_attribution(attribution: dict[str, Any]) -> None:
    print("收益归因分析（相对 Baseline 0 的年化超额收益）：")
    for key in ("hard_constraints", "factor_selection", "full_constraints"):
        value = attribution["contributions"][key]
        percentage = attribution["percentages"][key]
        pct = "—" if not np.isfinite(percentage) else f"{percentage:.1f}%"
        print(f"  {ATTRIBUTION_LABELS[key]}: {value:+.2%}（占比 {pct}）")
    print(f"  合计超额收益: {attribution['excess_return']:+.2%}（100%）")


def run(
    config: dict[str, Any], *, verbose: bool = False, tag: str | None = None,
    by_year: bool = False, parameter_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    backtest = config["backtest"]
    output_dir = ROOT / config["output"]["directory"]
    output_dir.mkdir(parents=True, exist_ok=True)
    params = strategy_params(config)
    safe_tag = re.sub(r"[^0-9A-Za-z_-]+", "_", str(tag or "")).strip("_")
    params.update(experiment_params(config, safe_tag))
    params.update(dict(parameter_overrides or {}))
    suffix = "after_fix" if safe_tag == "after_root_cause_fix" else safe_tag
    started = time.perf_counter()
    print(f"加载点时数据：{backtest['start_date']} ~ {backtest['end_date']} ...", flush=True)
    loader = VBTDataLoader(
        start_date=backtest["start_date"],
        end_date=backtest["end_date"],
        cache_enabled=False,
    )
    data_factors = list(DEFAULT_FACTORS)
    if bool(params.get("fusion_mode", False)):
        data_factors.extend(params.get("fusion_factors") or ())
        if bool(params.get("overheat_filter_enabled", True)):
            data_factors.append(str(params.get("overheat_factor", "reversal_5d")))
    data = loader.load(
        factors=tuple(dict.fromkeys(data_factors)),
        include_prices=True,
        include_volumes=True,
        include_market_cap=True,
        include_float_mv=True,
        include_is_st=True,
        include_listed_date=True,
        include_absolute_financials=True,
        adjusted_prices=bool(backtest.get("adjusted_prices", True)),
    )

    navs: dict[str, pd.Series] = {}
    metrics: dict[str, dict[str, float]] = {}
    summary_rows: list[dict[str, Any]] = []
    full_execution: dict[str, Any] = {}
    for position, name in enumerate(STRATEGY_NAMES, 1):
        print(f"[{position}/4] 运行 {LABELS[name]} ...", flush=True)
        result = _engine(data, AblationStrategy(name, params), backtest).run(verbose=verbose)
        values = _metrics(result)
        metrics[name] = values
        navs[name] = result.nav.astype(float)
        selected = result.metadata.get("selected_counts", {})
        eligible = result.metadata.get("eligible_counts", {})
        if name == "full":
            full_execution = {
                "volatility_filter_mode": result.metadata.get(
                    "volatility_filter_mode", "not_applicable"
                ),
                "rebalance_buffer_enabled": bool(
                    result.metadata.get("rebalance_buffer_enabled", False)
                ),
                "max_sells_per_rebalance": int(
                    result.metadata.get("max_sells_per_rebalance", 0)
                ),
                "rebalance_trades": result.metadata.get("rebalance_trades", {}),
                "candidate_stage_counts": result.metadata.get("candidate_stage_counts", {}),
                "core_candidate_symbols": result.metadata.get("core_candidate_symbols", {}),
                "selected_symbols": result.metadata.get("selected_symbols", {}),
                "expanded_symbols": result.metadata.get("expanded_symbols", {}),
                "invested_weights": result.metadata.get("invested_weights", {}),
                "turnover_by_date": result.metadata.get("turnover_by_date", {}),
                "fusion_mode": bool(result.metadata.get("fusion_mode", False)),
                "fusion_v2": bool(result.metadata.get("fusion_v2", False)),
                "fusion_factors": result.metadata.get("fusion_factors", []),
                "fusion_directions": result.metadata.get("fusion_directions", {}),
                "fusion_weights": result.metadata.get("fusion_weights", {}),
                "fusion_min_valid_factors": result.metadata.get(
                    "fusion_min_valid_factors"
                ),
                "overheat_factor": result.metadata.get("overheat_factor"),
                "hold_bonus": result.metadata.get("hold_bonus", 0.0),
                "cost_threshold": result.metadata.get("cost_threshold", 0.0),
                "overheat_excluded_counts": result.metadata.get(
                    "overheat_excluded_counts", {}
                ),
                "selected_counts": result.metadata.get("selected_counts", {}),
                "eligible_counts": result.metadata.get("eligible_counts", {}),
                "average_holdings": result.metadata.get("average_holdings"),
                "average_invested_weight": result.metadata.get(
                    "average_invested_weight"
                ),
                "fusion_candidate_n": result.metadata.get("fusion_candidate_n"),
                "max_holding": result.metadata.get("max_holding"),
            }
        summary_rows.append(
            {
                "strategy": name,
                "label": LABELS[name],
                "average_holdings": float(result.metadata.get("average_holdings", np.nan)),
                "min_holdings": min(selected.values()) if selected else np.nan,
                "max_holdings": max(selected.values()) if selected else np.nan,
                "average_eligible": float(np.mean(list(eligible.values()))) if eligible else np.nan,
                "average_invested_weight": float(
                    result.metadata.get("average_invested_weight", np.nan)
                ),
                "annual_one_way_turnover": values["turnover"],
                "rebalance_count": len(result.metadata.get("rebalance_dates", [])),
            }
        )
        print(
            f"  年化 {values['annual_return']:.2%} | 回撤 {values['max_drawdown']:.2%} | "
            f"夏普 {values['sharpe_ratio']:.2f}",
            flush=True,
        )
        del result
        gc.collect()

    nav_frame = pd.DataFrame(navs).sort_index()
    if nav_frame.isna().any().any():
        missing = nav_frame.isna().sum()
        raise AssertionError(f"净值序列存在缺失：{missing[missing.gt(0)].to_dict()}")
    holdings = pd.DataFrame(summary_rows)
    attribution = calculate_attribution(metrics)
    if not math.isclose(attribution["closure_error"], 0.0, abs_tol=1e-12):
        raise AssertionError(f"收益归因未闭合：{attribution['closure_error']}")

    reproducibility = reproducibility_snapshot(params, backtest)
    yearly_frame = pd.DataFrame()
    yearly_acceptance: dict[str, Any] = {}
    if by_year:
        yearly_frame, yearly_acceptance = calculate_yearly_performance(
            nav_frame, full_execution
        )
        yearly_frame.to_csv(
            output_dir / _artifact_name("yearly_results", suffix, "csv"),
            index=False,
            encoding="utf-8-sig",
        )
    payload = {
        "metrics": metrics,
        "attribution": attribution,
        "config": config,
        "experiment_params": {**experiment_params(config, safe_tag), **dict(parameter_overrides or {})},
        "holdings_summary": summary_rows,
        "full_execution": full_execution,
        "reproducibility": reproducibility,
        "tag": tag,
        "yearly_results": yearly_frame.to_dict(orient="records") if by_year else [],
        "yearly_acceptance": yearly_acceptance,
    }
    (output_dir / _artifact_name("metrics", suffix, "json")).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    nav_frame.rename_axis("trade_date").to_csv(
        output_dir / _artifact_name("nav_series", suffix, "csv"), encoding="utf-8-sig"
    )
    holdings.to_csv(
        output_dir / _artifact_name("holdings_summary", suffix, "csv"),
        index=False,
        encoding="utf-8-sig",
    )
    _plot_series(nav_frame, output_dir, suffix)
    markdown = _report_markdown(config, metrics, attribution, holdings)
    if by_year:
        markdown = f"{markdown.rstrip()}\n\n{_yearly_markdown(yearly_frame, yearly_acceptance)}"
    if suffix == "lowvol_band_buffer":
        markdown = markdown.replace(
            "# 约束消融实验报告", "# 低波 Band + 调仓缓冲回测报告", 1
        )
        appendix = _lowvol_acceptance_appendix(
            output_dir, metrics, holdings, full_execution
        )
        if appendix:
            markdown = f"{markdown.rstrip()}\n\n{appendix}"
    elif suffix == "rollback_top_buffer":
        markdown = markdown.replace(
            "# 约束消融实验报告", "# 低波 Top + 调仓缓冲回滚报告", 1
        )
        appendix = _rollback_acceptance_appendix(
            output_dir, metrics, holdings, full_execution
        )
        if appendix:
            markdown = f"{markdown.rstrip()}\n\n{appendix}"
    if suffix:
        markdown = markdown.replace("nav_comparison.png", _artifact_name("nav_comparison", suffix, "png"))
        markdown = markdown.replace(
            "drawdown_comparison.png", _artifact_name("drawdown_comparison", suffix, "png")
        )
    (output_dir / _artifact_name("ablation_report", suffix, "md")).write_text(
        markdown, encoding="utf-8"
    )
    html_report = _report_html(markdown, metrics, holdings)
    if suffix:
        html_report = html_report.replace(
            "nav_comparison.png", _artifact_name("nav_comparison", suffix, "png")
        ).replace(
            "drawdown_comparison.png",
            _artifact_name("drawdown_comparison", suffix, "png"),
        )
    (output_dir / _artifact_name("ablation_report", suffix, "html")).write_text(
        html_report, encoding="utf-8"
    )
    print_attribution(attribution)
    print(f"输出目录：{output_dir}")
    print(f"总耗时：{time.perf_counter() - started:.1f}s")
    return payload


def _plot_dividend_single_nav(nav_frame: pd.DataFrame, output_dir: Path) -> None:
    _configure_chinese_font()
    normalized = nav_frame.div(nav_frame.iloc[0])
    fig, ax = plt.subplots(figsize=(12, 6))
    normalized.rename(columns=LABELS).plot(ax=ax, linewidth=1.7)
    ax.set_title("股息率 Top30 单因子回测：与 Baseline 1 净值对比")
    ax.set_xlabel("日期")
    ax.set_ylabel("净值（起点=1）")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_dir / "nav_comparison_dividend_yield_only.png", dpi=160)
    plt.close(fig)


def run_dividend_yield_only(
    config: dict[str, Any], *, verbose: bool = False
) -> dict[str, Any]:
    """Compare the confirmed dividend Top30 diagnostic with Baseline 1."""
    backtest = config["backtest"]
    experiment = config.get("dividend_yield_only")
    if not isinstance(experiment, dict):
        raise ValueError("消融配置缺少 dividend_yield_only 分组")
    selection = experiment.get("selection", {})
    params = {
        **strategy_params(config),
        **selection,
    }
    params["top_n"] = int(selection.get("top_n", 30))
    output_dir = ROOT / config["output"]["directory"]
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    print(
        f"加载点时数据：{backtest['start_date']} ~ {backtest['end_date']} ...",
        flush=True,
    )
    loader = VBTDataLoader(
        start_date=backtest["start_date"],
        end_date=backtest["end_date"],
        cache_enabled=False,
    )
    data = loader.load(
        factors=DEFAULT_FACTORS,
        include_prices=True,
        include_volumes=True,
        include_market_cap=True,
        include_float_mv=True,
        include_is_st=True,
        include_listed_date=True,
        include_absolute_financials=True,
        adjusted_prices=bool(backtest.get("adjusted_prices", True)),
    )

    names = ("baseline1", "dividend_yield_only")
    metrics: dict[str, dict[str, float]] = {}
    navs: dict[str, pd.Series] = {}
    holdings: dict[str, float] = {}
    for position, name in enumerate(names, 1):
        print(f"[{position}/2] 运行 {LABELS[name]} ...", flush=True)
        result = _engine(data, AblationStrategy(name, params), backtest).run(verbose=verbose)
        values = _metrics(result)
        metrics[name] = values
        navs[name] = result.nav.astype(float)
        holdings[name] = float(result.metadata.get("average_holdings", np.nan))
        print(
            f"  年化 {values['annual_return']:.2%} | 回撤 {values['max_drawdown']:.2%} | "
            f"夏普 {values['sharpe_ratio']:.2f} | 换手 {values['turnover']:.2%}",
            flush=True,
        )
        del result
        gc.collect()

    nav_frame = pd.DataFrame(navs).sort_index()
    if nav_frame.isna().any().any():
        missing = nav_frame.isna().sum()
        raise AssertionError(f"净值序列存在缺失：{missing[missing.gt(0)].to_dict()}")
    _plot_dividend_single_nav(nav_frame, output_dir)

    baseline = metrics["baseline1"]["annual_return"]
    single = metrics["dividend_yield_only"]["annual_return"]
    effective = single > baseline
    conclusion = (
        "股息率 Top30 超过硬约束基线，该单因子扩容方案有效。"
        if effective
        else "股息率 Top30 未超过硬约束基线；该结果否定的是 Top30 扩容方案，不否定 Q5/Top10 极端分位的有效性。"
    )
    lines = [
        "# dividend_yield Top30 单因子回测",
        "",
        f"> 区间：{backtest['start_date']} ~ {backtest['end_date']}；月末信号、下一交易日执行。",
        "",
        "| 策略 | 年化收益 | 最大回撤 | 夏普 | 年均单边换手 | 平均持仓数 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in names:
        value = metrics[name]
        lines.append(
            f"| {LABELS[name]} | {_pct(value['annual_return'])} | "
            f"{_pct(value['max_drawdown'])} | {_number(value['sharpe_ratio'])} | "
            f"{_pct(value['turnover'])} | {_number(holdings[name], 1)} |"
        )
    lines.extend(
        [
            "",
            f"相对 Baseline 1 年化差值：**{single - baseline:+.2%}**。",
            "",
            f"**结论：{conclusion}**",
            "",
            "![净值对比](nav_comparison_dividend_yield_only.png)",
            "",
        ]
    )
    payload = {
        "metrics": metrics,
        "annual_return_difference_vs_baseline1": single - baseline,
        "top30_effective": effective,
        "effective": effective,
        "conclusion": conclusion,
        "config": config,
        "reproducibility": reproducibility_snapshot(params, backtest),
        "tag": "dividend_yield_only",
    }
    (output_dir / "metrics_dividend_yield_only.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    nav_frame.rename_axis("trade_date").to_csv(
        output_dir / "nav_series_dividend_yield_only.csv", encoding="utf-8-sig"
    )
    (output_dir / "ablation_report_dividend_yield_only.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    print(f"结论：{conclusion}")
    print(f"输出目录：{output_dir}")
    print(f"总耗时：{time.perf_counter() - started:.1f}s")
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="四组约束消融实验")
    parser.add_argument("--start", help="覆盖起始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="覆盖结束日期 YYYY-MM-DD")
    parser.add_argument("--config", default="config/vectorbt/ablation_config.yaml")
    parser.add_argument("--tag", help="结果标签；after_root_cause_fix 写入 *_after_fix 文件")
    parser.add_argument("--quick", action="store_true", help="读取最近结果，仅打印收益归因")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--by-year", action="store_true", help="输出连续回测的分年度绩效")
    parser.add_argument("--fusion", action="store_true", help="启用 experimental 融合排序")
    parser.add_argument("--equal-weight", action="store_true", help="在指定因子池内严格等权")
    parser.add_argument("--factors", help="逗号分隔的融合因子池")
    parser.add_argument("--dividend-weight", type=float, default=None)
    parser.add_argument("--volatility-weight", type=float, default=None)
    parser.add_argument("--fusion-candidate-n", type=int, default=None)
    parser.add_argument("--max-holding", type=int, default=None)
    parser.add_argument("--hold-bonus", type=float, default=None)
    parser.add_argument("--cost-threshold", type=float, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from market_data import configure_stdout_utf8

    configure_stdout_utf8()
    args = parse_args(argv)
    config = load_config(args.config)
    if args.start:
        config["backtest"]["start_date"] = args.start
    if args.end:
        config["backtest"]["end_date"] = args.end
    output_dir = ROOT / config["output"]["directory"]
    if args.quick:
        safe_tag = re.sub(r"[^0-9A-Za-z_-]+", "_", str(args.tag or "")).strip("_")
        suffix = "after_fix" if safe_tag == "after_root_cause_fix" else safe_tag
        path = output_dir / _artifact_name("metrics", suffix, "json")
        if not path.exists():
            raise FileNotFoundError(f"尚无消融结果，请先运行完整实验：{path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        print_attribution(payload["attribution"])
        return 0
    if args.tag == "dividend_yield_only":
        run_dividend_yield_only(config, verbose=args.verbose)
    else:
        overrides: dict[str, Any] = {}
        if args.fusion:
            overrides.update(
                fusion_cli_overrides(
                    factors=args.factors,
                    equal_weight=args.equal_weight,
                )
            )
        elif args.equal_weight or args.factors:
            parser.error("--equal-weight/--factors 必须与 --fusion 同时使用")
        if args.dividend_weight is not None:
            overrides["dividend_weight"] = args.dividend_weight
        if args.volatility_weight is not None:
            overrides["volatility_weight"] = args.volatility_weight
        if args.fusion_candidate_n is not None:
            overrides["fusion_candidate_n"] = args.fusion_candidate_n
        if args.max_holding is not None:
            overrides["max_holding"] = args.max_holding
        if args.hold_bonus is not None:
            overrides["hold_bonus"] = args.hold_bonus
        if args.cost_threshold is not None:
            overrides["cost_threshold"] = args.cost_threshold
        run(
            config,
            verbose=args.verbose,
            tag=args.tag,
            by_year=args.by_year or bool(args.fusion),
            parameter_overrides=overrides,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
