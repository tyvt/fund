#!/usr/bin/env python
"""Analyze Full-strategy turnover before and after the rebalance buffer."""

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


def analyze_candidate_pool(payload: dict[str, Any]) -> pd.DataFrame:
    """Return one row per rebalance with all candidate-pool stages."""
    execution = payload.get("full_execution", {})
    stages = execution.get("candidate_stage_counts", {})
    dates = sorted(
        set().union(*(set(values) for values in stages.values() if isinstance(values, dict)))
    )
    rows: list[dict[str, Any]] = []
    for date in dates:
        holdings = int(stages.get("holdings", {}).get(date, 0))
        final = int(stages.get("final", {}).get(date, 0))
        rows.append(
            {
                "date": pd.Timestamp(date),
                "hard": int(stages.get("hard", {}).get(date, 0)),
                "after_vol": int(stages.get("after_vol", {}).get(date, 0)),
                "after_div": int(stages.get("after_div", {}).get(date, 0)),
                "final": final,
                "holdings": holdings,
                "core_ratio": min(final, holdings) / holdings if holdings else 0.0,
                "scenario": (
                    "候选充裕" if final >= 30 else
                    "候选紧张" if final >= 10 else
                    "候选枯竭" if final >= 3 else "候选极枯"
                ),
            }
        )
    return pd.DataFrame(rows)


def _configure_font() -> None:
    for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")):
        if path.is_file():
            font_manager.fontManager.addfont(str(path))
            plt.rcParams["font.sans-serif"] = [
                font_manager.FontProperties(fname=str(path)).get_name()
            ]
            break
    plt.rcParams["axes.unicode_minus"] = False


def _linked_contribution(component: pd.Series, portfolio_return: pd.Series) -> float:
    """Geometrically link additive daily return contributions."""
    future_growth = (1.0 + portfolio_return.iloc[::-1]).cumprod().iloc[::-1]
    multiplier = future_growth.shift(-1, fill_value=1.0)
    return float((component * multiplier).sum())


def analyze_dynamic_attribution(payload: dict[str, Any]) -> dict[str, Any]:
    """Rerun Full once and attribute actual daily return to core/buffer holdings."""
    from vbt.adapters import DEFAULT_FACTORS, VBTDataLoader
    from vbt.strategies import AblationStrategy
    from scripts.run_ablation import _engine, experiment_params, strategy_params

    config = payload["config"]
    backtest = config["backtest"]
    params = strategy_params(config)
    params.update(experiment_params(config, payload.get("tag")))
    params.update(payload.get("experiment_params", {}))
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
    result = _engine(data, AblationStrategy("full", params), backtest).run()
    positions = result.positions.astype(float)
    close = data["close"].reindex(index=positions.index, columns=positions.columns).ffill()
    asset_returns = close.pct_change().fillna(0.0)
    prior_positions = positions.shift(1).fillna(0.0)

    core_by_date = result.metadata.get("core_candidate_symbols", {})
    rebalance_dates = sorted(pd.Timestamp(date) for date in core_by_date)
    core_mask = pd.DataFrame(False, index=positions.index, columns=positions.columns)
    for index, start in enumerate(rebalance_dates):
        stop = rebalance_dates[index + 1] if index + 1 < len(rebalance_dates) else None
        rows = core_mask.index >= start
        if stop is not None:
            rows &= core_mask.index < stop
        symbols = positions.columns.intersection(core_by_date[start.date().isoformat()])
        core_mask.loc[rows, symbols] = True
    prior_core = core_mask.shift(1, fill_value=False)
    weighted_returns = prior_positions * asset_returns
    core_raw = weighted_returns.where(prior_core, 0.0).sum(axis=1)
    buffer_raw = weighted_returns.where(~prior_core, 0.0).sum(axis=1)
    actual = result.nav.astype(float).pct_change().fillna(0.0)

    core_exposure = prior_positions.where(prior_core, 0.0).sum(axis=1)
    buffer_exposure = prior_positions.where(~prior_core, 0.0).sum(axis=1)
    exposure = (core_exposure + buffer_exposure).replace(0.0, np.nan)
    residual = actual - core_raw - buffer_raw
    core_daily = core_raw + residual * core_exposure.div(exposure).fillna(0.0)
    buffer_daily = buffer_raw + residual * buffer_exposure.div(exposure).fillna(0.0)
    # Initial formation or a fully-cash prior day has no stock exposure to use
    # as an allocation key. Treat that execution residual as buffer/fallback
    # cost so the requested two-column attribution closes to actual NAV.
    buffer_daily += actual - core_daily - buffer_daily
    closure_error = float((actual - core_daily - buffer_daily).abs().max())

    core_total = _linked_contribution(core_daily, actual)
    buffer_total = _linked_contribution(buffer_daily, actual)
    total_return = float(result.nav.iloc[-1] / result.nav.iloc[0] - 1.0)
    denominator = core_total + buffer_total
    core_ratio = core_total / denominator if not np.isclose(denominator, 0.0) else float("nan")
    buffer_ratio = buffer_total / denominator if not np.isclose(denominator, 0.0) else float("nan")
    annual_return = float(result.annual_return)
    daily = pd.DataFrame(
        {
            "portfolio_return": actual,
            "core_contribution": core_daily,
            "buffer_contribution": buffer_daily,
            "core_exposure": core_exposure,
            "buffer_exposure": buffer_exposure,
        }
    )
    return {
        "summary": {
            "core_total_return_contribution": core_total,
            "buffer_total_return_contribution": buffer_total,
            "core_contribution_ratio": core_ratio,
            "buffer_contribution_ratio": buffer_ratio,
            "core_annual_return_contribution": annual_return * core_ratio,
            "buffer_annual_return_contribution": annual_return * buffer_ratio,
            "buffer_dependency_pass": bool(buffer_ratio <= 0.30),
            "total_return": total_return,
            "annual_return": annual_return,
            "max_daily_closure_error": closure_error,
        },
        "daily": daily,
    }


def deep_analyze(
    result_path: str | Path,
    *,
    output_dir: str | Path = "output/robustness",
) -> dict[str, Any]:
    payload = _load(result_path)
    target = _resolve(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    candidates = analyze_candidate_pool(payload)
    if candidates.empty:
        raise ValueError("结果缺少候选池阶段明细，请先重新运行新版 run_ablation.py")
    candidates.to_csv(target / "candidate_pool_analysis.csv", index=False, encoding="utf-8-sig")
    scenario_frequency = candidates["scenario"].value_counts(normalize=True).to_dict()
    attribution = analyze_dynamic_attribution(payload)
    attribution["daily"].to_csv(
        target / "daily_core_buffer_attribution.csv", encoding="utf-8-sig"
    )
    (target / "core_buffer_attribution.json").write_text(
        json.dumps(attribution["summary"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _configure_font()
    fig, ax = plt.subplots(figsize=(12, 5))
    candidates.plot(x="date", y=["after_vol", "after_div", "final", "holdings"], ax=ax)
    ax.axhline(30, color="#2e7d32", linestyle="--", linewidth=1.1, label="充裕线 30")
    ax.axhline(9, color="#c62828", linestyle="--", linewidth=1.1, label="枯竭线 9")
    ax.set_title("候选池各阶段与实际持仓")
    ax.set_ylabel("股票数量")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(target / "candidate_pool_analysis.png", dpi=180)
    plt.close(fig)
    result = {
        "months": int(len(candidates)),
        "average_final_candidates": float(candidates["final"].mean()),
        "depletion_frequency": float(candidates["final"].le(9).mean()),
        "depletion_pass": bool(candidates["final"].le(9).mean() < 0.20),
        "scenario_frequency": scenario_frequency,
        **attribution["summary"],
    }
    (target / "candidate_pool_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分析调仓缓冲前后的换手率")
    parser.add_argument("--result", required=True, help="修复后 metrics JSON")
    parser.add_argument("--before", default="output/ablation/metrics_after_fix.json")
    parser.add_argument("--output", default="output/ablation/turnover_comparison.png")
    parser.add_argument("--deep", action="store_true", help="候选池阶段与逐日动态权重归因")
    parser.add_argument("--output-dir", default="output/robustness")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.deep:
        summary = deep_analyze(args.result, output_dir=args.output_dir)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"候选池深度分析：{_resolve(args.output_dir)}")
        return 0
    summary = analyze(args.result, before_path=args.before, output_path=args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"换手率对比图：{_resolve(args.output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
