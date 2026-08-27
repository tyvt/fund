#!/usr/bin/env python
"""Nine-item robustness validation for the official H30269 baseline."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from scripts.incremental_capital_test import simulate_injection
from scripts.run_h30269_baseline import load_adjusted_close


ROOT = Path(__file__).resolve().parents[1]


def _annualized(daily_returns: np.ndarray) -> float:
    values = np.asarray(daily_returns, dtype=float)
    values = values[np.isfinite(values) & (values > -1.0)]
    return float(np.exp(np.log1p(values).sum() * 252.0 / len(values)) - 1.0) if len(values) else float("nan")


def _summary(values: list[float]) -> dict[str, float]:
    data = np.asarray(values, dtype=float)
    data = data[np.isfinite(data)]
    return {
        "mean": float(np.mean(data)),
        "std": float(np.std(data, ddof=1)) if len(data) > 1 else 0.0,
        "p05": float(np.quantile(data, 0.05)),
        "p50": float(np.quantile(data, 0.50)),
        "p95": float(np.quantile(data, 0.95)),
    }


def _fixed_weight_path(
    returns: pd.DataFrame,
    events: list[tuple[pd.Timestamp, pd.Series]],
    shifts: np.ndarray | None = None,
) -> np.ndarray:
    dates = returns.index
    output = np.zeros(len(dates), dtype=float)
    locations: list[int] = []
    for index, (day, _) in enumerate(events):
        location = int(dates.searchsorted(day))
        if shifts is not None:
            location += int(shifts[index])
        locations.append(min(max(location, 0), len(dates) - 1))
    locations = list(np.maximum.accumulate(locations))
    for index, ((_, weights), start) in enumerate(zip(events, locations)):
        stop = locations[index + 1] if index + 1 < len(locations) else len(dates)
        if stop <= start:
            continue
        active = weights.index.intersection(returns.columns)
        if active.empty:
            continue
        w = weights.reindex(active).fillna(0.0).to_numpy(dtype=float)
        w /= w.sum()
        values = returns.iloc[start:stop][active].fillna(0.0).to_numpy(dtype=float)
        # Buy-and-hold within each annual segment.  A direct ``values @ w``
        # would silently rebalance every day and materially overstate the test.
        asset_growth = np.cumprod(1.0 + values, axis=0)
        segment_nav = asset_growth @ w
        previous = np.r_[1.0, segment_nav[:-1]]
        output[start:stop] = segment_nav / previous - 1.0
    return output


def _random_drop_test(
    returns: pd.DataFrame,
    events: list[tuple[pd.Timestamp, pd.Series]],
    iterations: int,
) -> dict[str, float]:
    rng = np.random.default_rng(20260827)
    values: list[float] = []
    for _ in range(iterations):
        dropped: list[tuple[pd.Timestamp, pd.Series]] = []
        for day, weights in events:
            keep = max(1, int(round(len(weights) * 0.8)))
            symbols = rng.choice(weights.index.to_numpy(), size=keep, replace=False)
            selected = weights.reindex(symbols)
            dropped.append((day, selected / selected.sum()))
        values.append(_annualized(_fixed_weight_path(returns, dropped)))
    return _summary(values)


def _shift_test(
    returns: pd.DataFrame,
    events: list[tuple[pd.Timestamp, pd.Series]],
    iterations: int,
) -> dict[str, float]:
    rng = np.random.default_rng(20260828)
    values = [
        _annualized(
            _fixed_weight_path(
                returns, events, rng.integers(-5, 6, size=len(events), dtype=np.int32)
            )
        )
        for _ in range(iterations)
    ]
    return _summary(values)


def _bootstrap(nav: pd.Series, iterations: int, block: int = 20) -> dict[str, float]:
    daily = nav.pct_change().dropna().to_numpy(dtype=float)
    rng = np.random.default_rng(20260829)
    max_start = max(len(daily) - block, 0)
    values: list[float] = []
    blocks = math.ceil(len(daily) / block)
    for _ in range(iterations):
        starts = rng.integers(0, max_start + 1, size=blocks)
        sample = np.concatenate([daily[start : start + block] for start in starts])[: len(daily)]
        values.append(_annualized(sample))
    return _summary(values)


def _markdown(result: Mapping[str, Any]) -> str:
    status = lambda value: "PASS" if value else "FAIL"
    checks = result["checks"]
    details = result["details"]
    lines = [
        "# H30269 基础层九项综合验收",
        "",
        f"结论：通过 **{result['passed']}/9** 项；规则门槛 6/9。综合状态：**{'通过' if result['accepted'] else '不通过'}**。",
        "",
        "| 序号 | 验收项 | 实测 | 门槛 | 状态 |",
        "|---:|---|---:|---:|:---:|",
        f"| 1 | 年度稳定性 | {details['annual_std']:.2%} | <15% | {status(checks['annual_stability'])} |",
        f"| 2 | 候选池健康度 | 枯竭频率 {details['depletion_frequency']:.2%} | <20% | {status(checks['candidate_pool_health'])} |",
        f"| 3 | 缓冲依赖 | {details['buffer_dependency']:.2%} | ≤30% | {status(checks['buffer_dependency'])} |",
        f"| 4 | 随机剔除20% | P5 {details['random_drop']['p05']:.2%} | >5% | {status(checks['random_drop_20pct'])} |",
        f"| 5 | 调仓偏移±5日 | P5 {details['rebalance_shift']['p05']:.2%} | >5% | {status(checks['rebalance_shift'])} |",
        f"| 6 | 滚动3年 | 均值 {details['rolling_3y']['mean']:.2%}；最小 {details['rolling_3y']['min']:.2%} | 均值>5%，全部>-5% | {status(checks['rolling_3y'])} |",
        f"| 7 | 官方两步串联 | 最小候选 {details['minimum_candidates']:.0f}；年化 {details['annual_return']:.2%} | ≥30、≥10% | {status(checks['official_two_stage'])} |",
        f"| 8 | Block Bootstrap | P5 {details['bootstrap']['p05']:.2%} | ≥0% | {status(checks['block_bootstrap'])} |",
        f"| 9 | 增量资金 | {details['capital_passed']}/3 | 全部通过 | {status(checks['incremental_capital'])} |",
        "",
        "## 解释",
        "",
        "第 3 项衡量最终样本中因官网 20% 调整比例限制而保留、但不在当期纯两步 Top50 内的占比；这是官方机制依赖，不是额外策略缓冲。第 7 项只验证官网‘股息率前75→低波50’串联，不采用 rank-sum 或行业内排名。",
        "",
    ]
    return "\n".join(lines)


def run_baseline_validation(tag: str, *, iterations: int = 1000) -> dict[str, Any]:
    metrics_path = ROOT / "output/baseline" / f"metrics_{tag}.json"
    nav_path = ROOT / "output/baseline" / f"nav_{tag}.csv"
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    nav_frame = pd.read_csv(nav_path, index_col="date", parse_dates=True)
    nav = nav_frame["strategy_nav"].astype(float)
    snapshots = payload["snapshots"]
    symbols = sorted({symbol for item in snapshots for symbol in item["final_selection"]})
    close = load_adjusted_close(symbols, str(nav.index.min().date()), str(nav.index.max().date()))
    close = close.reindex(nav.index).ffill()
    returns = close.pct_change(fill_method=None)
    events = [
        (pd.Timestamp(item["effective_date"]), pd.Series(item["weights"], dtype=float))
        for item in snapshots
        if pd.Timestamp(item["effective_date"]) <= nav.index.max()
    ]

    yearly = nav.resample("YE").last().pct_change().dropna()
    annual_std = float(yearly.std(ddof=1))
    annual_pass = annual_std < 0.15
    depleted = [
        item["stage_counts"]["eligible_before_top75"] < 75
        or item["stage_counts"]["volatility_available"] < 50
        for item in snapshots
    ]
    depletion_frequency = float(np.mean(depleted))
    candidate_pass = depletion_frequency < 0.20
    buffer_dependency = float(np.mean([item["buffer_dependency"] for item in snapshots[1:]]))
    buffer_pass = buffer_dependency <= 0.30

    random_drop = _random_drop_test(returns, events, iterations)
    shift = _shift_test(returns, events, iterations)
    random_pass = random_drop["p05"] > 0.05
    shift_pass = shift["p05"] > 0.05

    daily = nav.pct_change().dropna()
    rolling_values: list[float] = []
    for start_year in range(nav.index.min().year, nav.index.max().year - 1):
        sample = daily[(daily.index >= pd.Timestamp(start_year, 1, 1)) & (daily.index < pd.Timestamp(start_year + 3, 1, 1))]
        if len(sample) >= 252 * 2.5:
            rolling_values.append(_annualized(sample.to_numpy()))
    rolling = {"values": rolling_values, "mean": float(np.mean(rolling_values)), "min": float(np.min(rolling_values))}
    rolling_pass = rolling["mean"] > 0.05 and rolling["min"] > -0.05
    minimum_candidates = min(item["stage_counts"]["eligible_before_top75"] for item in snapshots)
    annual_return = float(payload["metrics"]["strategy_nav"]["annual_return"])
    two_stage_pass = minimum_candidates >= 30 and annual_return >= 0.10
    bootstrap = _bootstrap(nav, iterations)
    bootstrap_pass = bootstrap["p05"] >= 0.0

    previous, target = snapshots[-2]["final_selection"], snapshots[-1]["final_selection"]
    capital = [
        simulate_injection(previous, target, current_value=100000.0, new_capital=value)
        for value in (100000.0, 200000.0, 500000.0)
    ]
    capital_passed = sum(bool(item["passed"]) for item in capital)
    capital_pass = capital_passed == 3
    checks = {
        "annual_stability": bool(annual_pass),
        "candidate_pool_health": bool(candidate_pass),
        "buffer_dependency": bool(buffer_pass),
        "random_drop_20pct": bool(random_pass),
        "rebalance_shift": bool(shift_pass),
        "rolling_3y": bool(rolling_pass),
        "official_two_stage": bool(two_stage_pass),
        "block_bootstrap": bool(bootstrap_pass),
        "incremental_capital": bool(capital_pass),
    }
    passed = sum(checks.values())
    hard_metrics_pass = (
        annual_return >= 0.10
        and float(payload["metrics"]["strategy_nav"]["max_drawdown"]) >= -0.40
    )
    result: dict[str, Any] = {
        "tag": tag,
        "passed": passed,
        "total": 9,
        "checks": checks,
        "hard_metrics_pass": hard_metrics_pass,
        "accepted": passed >= 6 and hard_metrics_pass,
        "details": {
            "annual_std": annual_std,
            "depletion_frequency": depletion_frequency,
            "buffer_dependency": buffer_dependency,
            "random_drop": random_drop,
            "rebalance_shift": shift,
            "rolling_3y": rolling,
            "minimum_candidates": minimum_candidates,
            "annual_return": annual_return,
            "bootstrap": bootstrap,
            "capital_passed": capital_passed,
            "capital_scenarios": [{key: value for key, value in item.items() if key != "days"} for item in capital],
        },
    }
    output = ROOT / "output/validation"
    output.mkdir(parents=True, exist_ok=True)
    (output / "comprehensive_results_baseline.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "comprehensive_report_baseline.md").write_text(_markdown(result), encoding="utf-8")
    return result


__all__ = ["run_baseline_validation"]
