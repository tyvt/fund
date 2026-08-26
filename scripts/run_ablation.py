#!/usr/bin/env python
"""Run the four-way point-in-time constraint ablation experiment."""

from __future__ import annotations

import argparse
import gc
import html
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
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
}
ATTRIBUTION_LABELS = {
    "hard_constraints": "硬约束贡献",
    "factor_selection": "因子选股贡献",
    "full_constraints": "完整约束贡献",
}


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
        "rebalance_month": config["backtest"]["rebalance_month"],
        "rebalance_day": config["backtest"].get("rebalance_day", 15),
        "alignment_mode": False,
    }


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


def _plot_series(navs: pd.DataFrame, output_dir: Path) -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    normalized = navs.div(navs.iloc[0])
    fig, ax = plt.subplots(figsize=(12, 6))
    normalized.rename(columns=LABELS).plot(ax=ax, linewidth=1.6)
    ax.set_title("约束消融实验：四组策略净值对比")
    ax.set_xlabel("日期")
    ax.set_ylabel("净值（起点=1）")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_dir / "nav_comparison.png", dpi=160)
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
    fig.savefig(output_dir / "drawdown_comparison.png", dpi=160)
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
    exposure_text = (
        f"Full 平均持仓 {float(full_row['average_holdings']):.1f} 只、平均投资比例"
        f" {float(full_row['average_invested_weight']):.1%}（最少持仓"
        f" {int(full_row['min_holdings'])} 只），因此回撤改善有相当部分来自现金暴露，"
        "不能全部解释为股票筛选能力。"
    )
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
        f"- 调仓：每年 1 月 15 日后的首个交易日；信号取前一交易日",
        "- 执行：可分股目标权重；比例佣金、卖出印花税及滑点；不使用最低佣金和整手约束",
        "- 收益：前复权价格；避免除权除息被误记为投资亏损",
        "- 财务约束：绝对 ROE/资产负债率，年报自次年 4 月 30 日起可用",
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


def print_attribution(attribution: dict[str, Any]) -> None:
    print("收益归因分析（相对 Baseline 0 的年化超额收益）：")
    for key in ("hard_constraints", "factor_selection", "full_constraints"):
        value = attribution["contributions"][key]
        percentage = attribution["percentages"][key]
        pct = "—" if not np.isfinite(percentage) else f"{percentage:.1f}%"
        print(f"  {ATTRIBUTION_LABELS[key]}: {value:+.2%}（占比 {pct}）")
    print(f"  合计超额收益: {attribution['excess_return']:+.2%}（100%）")


def run(config: dict[str, Any], *, verbose: bool = False) -> dict[str, Any]:
    backtest = config["backtest"]
    output_dir = ROOT / config["output"]["directory"]
    output_dir.mkdir(parents=True, exist_ok=True)
    params = strategy_params(config)
    started = time.perf_counter()
    print(f"加载点时数据：{backtest['start_date']} ~ {backtest['end_date']} ...", flush=True)
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

    navs: dict[str, pd.Series] = {}
    metrics: dict[str, dict[str, float]] = {}
    summary_rows: list[dict[str, Any]] = []
    for position, name in enumerate(STRATEGY_NAMES, 1):
        print(f"[{position}/4] 运行 {LABELS[name]} ...", flush=True)
        result = _engine(data, AblationStrategy(name, params), backtest).run(verbose=verbose)
        values = _metrics(result)
        metrics[name] = values
        navs[name] = result.nav.astype(float)
        selected = result.metadata.get("selected_counts", {})
        eligible = result.metadata.get("eligible_counts", {})
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
    payload = {
        "metrics": metrics,
        "attribution": attribution,
        "config": config,
        "reproducibility": reproducibility,
    }
    (output_dir / "metrics.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    nav_frame.rename_axis("trade_date").to_csv(output_dir / "nav_series.csv", encoding="utf-8-sig")
    holdings.to_csv(output_dir / "holdings_summary.csv", index=False, encoding="utf-8-sig")
    _plot_series(nav_frame, output_dir)
    markdown = _report_markdown(config, metrics, attribution, holdings)
    (output_dir / "ablation_report.md").write_text(markdown, encoding="utf-8")
    (output_dir / "ablation_report.html").write_text(
        _report_html(markdown, metrics, holdings), encoding="utf-8"
    )
    print_attribution(attribution)
    print(f"输出目录：{output_dir}")
    print(f"总耗时：{time.perf_counter() - started:.1f}s")
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="四组约束消融实验")
    parser.add_argument("--start", help="覆盖起始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="覆盖结束日期 YYYY-MM-DD")
    parser.add_argument("--config", default="config/vectorbt/ablation_config.yaml")
    parser.add_argument("--quick", action="store_true", help="读取最近结果，仅打印收益归因")
    parser.add_argument("--verbose", action="store_true")
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
        path = output_dir / "metrics.json"
        if not path.exists():
            raise FileNotFoundError(f"尚无消融结果，请先运行完整实验：{path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        print_attribution(payload["attribution"])
        return 0
    run(config, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
