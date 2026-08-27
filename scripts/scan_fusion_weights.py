#!/usr/bin/env python
"""Scan fusion weights and 50/100/150 candidate-pool widths."""

from __future__ import annotations

import argparse
import json
import sys
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
from vbt.engine import VBTEngine
from vbt.engine.parameter_scan import ParameterScan
from vbt.strategies import AblationStrategy
from scripts.run_ablation import load_config, strategy_params


def _configure_font() -> None:
    for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")):
        if path.is_file():
            font_manager.fontManager.addfont(str(path))
            plt.rcParams["font.sans-serif"] = [font_manager.FontProperties(fname=str(path)).get_name()]
            break
    plt.rcParams["axes.unicode_minus"] = False


def scan_fusion_weights(
    *,
    config: dict[str, Any],
    dividend_weights: list[float],
    candidate_sizes: list[int],
) -> pd.DataFrame:
    backtest = config["backtest"]
    params = strategy_params(config)
    params.update(
        {
            "fusion_mode": True,
            "fusion_status": "experimental",
            "max_holding": 13,
        }
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
    engine = VBTEngine(
        data=data,
        strategy=AblationStrategy("full", params),
        initial_capital=float(backtest["initial_capital"]),
        commission=float(backtest["commission"]),
        min_commission=0.0,
        stamp_duty_before=float(backtest["stamp_duty"]),
        stamp_duty_after=float(backtest["stamp_duty"]),
        slippage=float(backtest["slippage"]),
        backtest_config=backtest,
    )
    simulator = ParameterScan(engine=engine, param_grid={})
    rows: list[dict[str, Any]] = []
    combinations = [
        (float(weight), 1.0 - float(weight), int(size))
        for size in candidate_sizes
        for weight in dividend_weights
    ]
    for index, (dividend_weight, volatility_weight, candidate_n) in enumerate(combinations, 1):
        print(
            f"[{index}/{len(combinations)}] dividend={dividend_weight:.2f}, "
            f"volatility={volatility_weight:.2f}, candidates={candidate_n}",
            flush=True,
        )
        overrides = {
            "fusion_mode": True,
            "dividend_weight": dividend_weight,
            "volatility_weight": volatility_weight,
            "fusion_candidate_n": candidate_n,
            "max_holding": 13,
        }
        try:
            targets, metadata = engine.strategy.with_params(overrides).generate_signals(data)
            metrics = simulator._simulate_targets(targets)
            eligible = list(metadata.get("eligible_counts", {}).values())
            holdings = list(metadata.get("selected_counts", {}).values())
            invested = list(metadata.get("invested_weights", {}).values())
            rows.append(
                {
                    "dividend_weight": dividend_weight,
                    "volatility_weight": volatility_weight,
                    "fusion_candidate_n": candidate_n,
                    "annual_return": metrics["annual_return"],
                    "sharpe": metrics["sharpe_ratio"],
                    "max_drawdown": metrics["max_drawdown"],
                    "turnover": float(metadata.get("turnover", np.nan)),
                    "avg_candidates": float(np.mean(eligible)) if eligible else float("nan"),
                    "avg_holdings": float(np.mean(holdings)) if holdings else float("nan"),
                    "max_holdings": int(max(holdings)) if holdings else 0,
                    "avg_invested_weight": float(np.mean(invested)) if invested else float("nan"),
                    "status": "ok",
                    "error": None,
                }
            )
        except Exception as exc:
            print(f"  ERROR {type(exc).__name__}: {exc}", flush=True)
            rows.append(
                {
                    "dividend_weight": dividend_weight,
                    "volatility_weight": volatility_weight,
                    "fusion_candidate_n": candidate_n,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return pd.DataFrame(rows)


def summarize_scan(results: pd.DataFrame) -> dict[str, Any]:
    def clean_record(row: pd.Series) -> dict[str, Any]:
        record: dict[str, Any] = {}
        for key, value in row.to_dict().items():
            if pd.isna(value):
                record[key] = None
            elif isinstance(value, np.generic):
                record[key] = value.item()
            else:
                record[key] = value
        return record

    valid = results.loc[results["status"].eq("ok")].copy()
    if valid.empty:
        raise RuntimeError("融合扫描没有成功的参数组合")
    best_return = clean_record(valid.loc[valid["annual_return"].idxmax()])
    best_sharpe = clean_record(valid.loc[valid["sharpe"].idxmax()])
    at_100 = valid.loc[valid["fusion_candidate_n"].eq(100)]
    recommended = (
        clean_record(at_100.loc[at_100["sharpe"].idxmax()])
        if not at_100.empty else best_sharpe
    )
    sensitivity_frame = valid.loc[
        np.isclose(valid["dividend_weight"], float(recommended["dividend_weight"]))
    ].sort_values("fusion_candidate_n")
    if sensitivity_frame.empty:
        sensitivity = []
        plateau = False
    else:
        sensitivity = [clean_record(row) for _, row in sensitivity_frame.iterrows()]
        row100 = sensitivity_frame.loc[sensitivity_frame["fusion_candidate_n"].eq(100)]
        best_width_return = float(sensitivity_frame["annual_return"].max())
        plateau = bool(
            not row100.empty
            and float(row100.iloc[0]["annual_return"]) >= best_width_return - 0.02
            and int(row100.iloc[0]["max_holdings"]) <= 13
        )
    return {
        "best_by_return": best_return,
        "best_by_sharpe": best_sharpe,
        "recommended_candidate_100": recommended,
        "candidate_width_sensitivity_at_recommended_weight": sensitivity,
        "candidate_100_plateau_pass": plateau,
        "plateau_definition": "100只候选的年化收益距50/100/150三档最优不超过2个百分点，且持仓不超过13只",
    }


def plot_heatmap(results: pd.DataFrame, output: Path) -> None:
    valid = results.loc[results["status"].eq("ok")]
    pivot = valid.pivot(
        index="fusion_candidate_n", columns="dividend_weight", values="annual_return"
    ).sort_index()
    _configure_font()
    fig, ax = plt.subplots(figsize=(11, 4.8))
    image = ax.imshow(pivot.to_numpy(), aspect="auto", cmap="RdYlGn")
    ax.set_xticks(range(len(pivot.columns)), [f"{value:.2f}" for value in pivot.columns])
    ax.set_yticks(range(len(pivot.index)), [str(value) for value in pivot.index])
    ax.set_xlabel("股息率权重（低波权重 = 1 - 股息率权重）")
    ax.set_ylabel("融合候选池宽度")
    ax.set_title("融合排序年化收益敏感性")
    for row in range(len(pivot.index)):
        for column in range(len(pivot.columns)):
            value = float(pivot.iloc[row, column])
            ax.text(column, row, f"{value:.1%}", ha="center", va="center", fontsize=8)
    fig.colorbar(image, ax=ax, label="年化收益")
    fig.tight_layout()
    fig.savefig(output, dpi=180)
    plt.close(fig)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="融合排序权重与候选池宽度扫描")
    parser.add_argument("--config", default="config/vectorbt/ablation_config.yaml")
    parser.add_argument("--weights", default="0.30,0.35,0.40,0.45,0.50,0.55,0.60,0.65,0.70")
    parser.add_argument("--candidate-sizes", default="50,100,150")
    parser.add_argument("--reuse-results", action="store_true", help="复用已有27组CSV，仅重建摘要与热力图")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    weights = [float(value) for value in args.weights.split(",")]
    sizes = [int(value) for value in args.candidate_sizes.split(",")]
    output = ROOT / "output/robustness"
    output.mkdir(parents=True, exist_ok=True)
    result_path = output / "fusion_scan_results.csv"
    if args.reuse_results:
        if not result_path.is_file():
            raise FileNotFoundError(f"找不到可复用扫描结果：{result_path}")
        results = pd.read_csv(result_path)
    else:
        results = scan_fusion_weights(
            config=config, dividend_weights=weights, candidate_sizes=sizes
        )
        results.to_csv(result_path, index=False, encoding="utf-8-sig")
    summary = summarize_scan(results)
    (output / "fusion_scan_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    plot_heatmap(results, output / "fusion_scan_heatmap.png")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"融合扫描输出：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
