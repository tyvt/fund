#!/usr/bin/env python
"""Run the confirmed three-level transaction-cost ablation for Full."""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_ablation import _engine, _metrics, load_config, strategy_params
from vbt.adapters import DEFAULT_FACTORS, VBTDataLoader
from vbt.config import reproducibility_snapshot
from vbt.strategies import AblationStrategy


VARIANTS = ("full_no_cost", "full_low_cost", "full_high_cost")
LABELS = {
    "full_no_cost": "无成本",
    "full_low_cost": "低成本",
    "full_high_cost": "当前成本",
}


def _cost_backtest(config: dict[str, Any], name: str) -> dict[str, Any]:
    variant = config.get(name)
    if not isinstance(variant, dict):
        raise ValueError(f"消融配置缺少 {name} 分组")
    result = dict(config["backtest"])
    for key in ("commission", "slippage", "stamp_duty"):
        if key not in variant:
            raise ValueError(f"{name} 缺少成本参数 {key}")
        result[key] = float(variant[key])
    return result


def _pct(value: float) -> str:
    return "—" if not np.isfinite(value) else f"{value:.2%}"


def plot_cost_impact(
    nav_frame: pd.DataFrame, erosion: dict[str, float], output_dir: Path
) -> None:
    for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")):
        if path.is_file():
            font_manager.fontManager.addfont(str(path))
            plt.rcParams["font.sans-serif"] = [font_manager.FontProperties(fname=str(path)).get_name()]
            break
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))
    nav_frame.div(nav_frame.iloc[0]).rename(columns=LABELS).plot(ax=axes[0], linewidth=1.6)
    axes[0].set_title("Full 成本消融净值")
    axes[0].set_xlabel("日期")
    axes[0].set_ylabel("净值（起点=1）")
    axes[0].grid(True, alpha=0.3)
    bars = pd.Series(erosion).rename(index=LABELS).mul(100.0)
    bars.plot.bar(ax=axes[1], color=["#7aa6c2", "#e0a458", "#c94c4c"])
    axes[1].axhline(1.5, color="black", linestyle="--", linewidth=1.2, label="敏感阈值 1.5pct")
    axes[1].set_title("相对无成本版的年化收益侵蚀")
    axes[1].set_xlabel("")
    axes[1].set_ylabel("百分点")
    axes[1].tick_params(axis="x", rotation=0)
    axes[1].legend()
    axes[1].grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_dir / "cost_impact_analysis.png", dpi=160)
    plt.close(fig)


def run_cost_ablation(config: dict[str, Any], *, verbose: bool = False) -> dict[str, Any]:
    base_backtest = config["backtest"]
    output_dir = ROOT / "output" / "cost"
    output_dir.mkdir(parents=True, exist_ok=True)
    params = strategy_params(config)
    started = time.perf_counter()
    print(
        f"加载点时数据：{base_backtest['start_date']} ~ {base_backtest['end_date']} ...",
        flush=True,
    )
    loader = VBTDataLoader(
        start_date=base_backtest["start_date"],
        end_date=base_backtest["end_date"],
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
        adjusted_prices=bool(base_backtest.get("adjusted_prices", True)),
    )

    metrics: dict[str, dict[str, float]] = {}
    navs: dict[str, pd.Series] = {}
    applied_costs: dict[str, dict[str, float]] = {}
    for position, name in enumerate(VARIANTS, 1):
        backtest = _cost_backtest(config, name)
        applied_costs[name] = {
            key: float(backtest[key]) for key in ("commission", "slippage", "stamp_duty")
        }
        print(f"[{position}/3] 运行 {LABELS[name]} Full ...", flush=True)
        result = _engine(data, AblationStrategy("full", params), backtest).run(verbose=verbose)
        values = _metrics(result)
        metrics[name] = values
        navs[name] = result.nav.astype(float)
        print(
            f"  年化 {values['annual_return']:.2%} | 回撤 {values['max_drawdown']:.2%} | "
            f"换手 {values['turnover']:.2%}",
            flush=True,
        )
        del result
        gc.collect()

    nav_frame = pd.DataFrame(navs).sort_index()
    if nav_frame.isna().any().any():
        missing = nav_frame.isna().sum()
        raise AssertionError(f"成本消融净值存在缺失：{missing[missing.gt(0)].to_dict()}")
    no_cost_annual = metrics["full_no_cost"]["annual_return"]
    erosion = {
        name: float(no_cost_annual - metrics[name]["annual_return"]) for name in VARIANTS
    }
    current_erosion = erosion["full_high_cost"]
    sensitive = current_erosion > 0.015
    conclusion = (
        f"Full 对交易成本敏感；当前成本侵蚀年化收益 {current_erosion:.2%}，超过 1.5 个百分点。"
        if sensitive
        else f"Full 对交易成本不敏感；当前成本侵蚀年化收益 {current_erosion:.2%}，未超过 1.5 个百分点。"
    )

    plot_cost_impact(nav_frame, erosion, output_dir)

    lines = [
        "# Full 策略交易成本消融",
        "",
        f"> 区间：{base_backtest['start_date']} ~ {base_backtest['end_date']}；无成本版清零佣金、滑点和印花税，低成本版保留印花税。",
        "",
        "| 版本 | 佣金 | 滑点 | 卖出印花税 | 年化收益 | 最大回撤 | 年均单边换手 | 年化收益侵蚀 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in VARIANTS:
        values = metrics[name]
        costs = applied_costs[name]
        lines.append(
            f"| {LABELS[name]} | {_pct(costs['commission'])} | {_pct(costs['slippage'])} | "
            f"{_pct(costs['stamp_duty'])} | {_pct(values['annual_return'])} | "
            f"{_pct(values['max_drawdown'])} | {_pct(values['turnover'])} | {_pct(erosion[name])} |"
        )
    lines.extend(
        [
            "",
            f"**结论：{conclusion}**",
            "",
            "![成本影响](cost_impact_analysis.png)",
            "",
        ]
    )
    payload = {
        "metrics": metrics,
        "cost_parameters": applied_costs,
        "annual_return_erosion_vs_no_cost": erosion,
        "sensitivity_threshold": 0.015,
        "cost_sensitive": sensitive,
        "conclusion": conclusion,
        "config": config,
        "reproducibility": reproducibility_snapshot(params, base_backtest),
    }
    (output_dir / "metrics_cost_ablation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    nav_frame.rename_axis("trade_date").to_csv(
        output_dir / "nav_series_cost_ablation.csv", encoding="utf-8-sig"
    )
    (output_dir / "cost_ablation_report.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    print(f"结论：{conclusion}")
    print(f"输出目录：{output_dir}")
    print(f"总耗时：{time.perf_counter() - started:.1f}s")
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Full 策略交易成本消融")
    parser.add_argument("--config", default="config/vectorbt/ablation_config.yaml")
    parser.add_argument("--start", help="覆盖起始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="覆盖结束日期 YYYY-MM-DD")
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
    run_cost_ablation(config, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
