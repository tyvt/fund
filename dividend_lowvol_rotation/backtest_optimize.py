# -*- coding: utf-8 -*-
"""红利低波轮动：参数筛选 + 网格 + 贝叶斯优化。

训练段 / 验证段分离，目标函数侧重验证集相对 H30269 与买入持有的超额收益。

用法
----
    python -m dividend_lowvol_rotation.backtest_optimize --task all --years 10
    python -m dividend_lowvol_rotation.backtest_optimize --task screen
    python -m dividend_lowvol_rotation.backtest_optimize --task grid
    python -m dividend_lowvol_rotation.backtest_optimize --task bayesian --trials 50
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from dividend_lowvol_rotation.backtest import (
    BacktestContext,
    default_start_years,
    prepare_backtest_context,
    run_backtest,
)
from dividend_lowvol_rotation.backtest_validate import (
    _window_return,
    iter_annual_windows,
    load_index_benchmark_nav,
)
from dividend_lowvol_rotation.config import (
    BACKTEST_INITIAL_CAPITAL,
    BACKTEST_OUTPUT_DIR,
    BACKTEST_PREFETCH_SIZE,
)
from dividend_lowvol_rotation.strategy_params import StrategyParams, defaults
from market_data import configure_stdout_utf8

OUTPUT_STEM = "optimize"
DEFAULT_VALID_START = "2021-01-01"
SCREEN_EDGE_THRESHOLD = 0.5  # 验证集利差变化 < 0.5% 视为无影响

ATR_GRID: dict[str, list[Any]] = {
    "stop_atr_multiplier": [2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5],
}

COARSE_GRID: dict[str, list[Any]] = {
    "top_n": [5, 10, 15],
    "rebalance_days": [15, 20, 30],
    "sell_rank_multiplier": [1.5, 2.0, 2.5],
}

BAYESIAN_BOUNDS: dict[str, tuple[float, float]] = {
    "yield_rank_weight": (0.5, 2.0),
    "vol_rank_weight": (0.25, 1.5),
    "min_dividend_yield_pct": (1.5, 4.0),
    "market_vol_median_mult": (1.0, 2.5),
    "min_roe_pct": (0.0, 12.0),
    "max_industry_weight": (0.25, 0.50),
}

SCREEN_SPECS: list[dict[str, Any]] = [
    {"key": "top_n", "label": "持仓数", "values": [5, 10, 15, 20]},
    {"key": "rebalance_days", "label": "调仓周期", "values": [10, 15, 20, 30, 40]},
    {"key": "sell_rank_multiplier", "label": "卖出缓冲倍数", "values": [1.5, 2.0, 2.5, 3.0]},
    {"key": "yield_rank_weight", "label": "股息率权重", "values": [0.5, 1.0, 1.5, 2.0]},
    {"key": "vol_rank_weight", "label": "低波权重", "values": [0.25, 0.5, 1.0, 1.5]},
    {"key": "min_dividend_yield_pct", "label": "最低股息率%", "values": [1.5, 2.0, 2.5, 3.0]},
    {"key": "market_vol_median_mult", "label": "波动上限倍数", "values": [1.0, 1.5, 2.0, 2.5]},
    {"key": "min_roe_pct", "label": "最低ROE%", "values": [0.0, 5.0, 8.0, 12.0]},
    {"key": "max_industry_weight", "label": "行业上限", "values": [0.25, 0.34, 0.50]},
]


@dataclass
class TrialMetrics:
    score: float
    valid_edge_vs_index_pct: float | None
    valid_edge_vs_hold_pct: float | None
    train_edge_vs_index_pct: float | None
    train_edge_vs_hold_pct: float | None
    full_edge_vs_index_pct: float | None
    full_edge_vs_hold_pct: float | None
    valid_wfa_win_rate_vs_index: float | None
    strategy_return_pct: float | None
    hold_return_pct: float | None
    index_return_pct: float | None
    max_drawdown_pct: float | None


@dataclass
class TrialResult:
    trial_id: int
    params: dict[str, Any]
    metrics: TrialMetrics
    elapsed_sec: float = 0.0
    label: str = ""
    task: str = ""


@dataclass
class ScreenRow:
    param_key: str
    label: str
    value: Any
    edge_delta_vs_index_pct: float | None
    edge_delta_vs_hold_pct: float | None
    score_delta: float | None
    verdict: str


def _fmt_pct(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%"


def _period_edge(
    strat_nav: pd.DataFrame,
    other_nav: pd.DataFrame,
    w_start: str,
    w_end: str,
    initial_capital: float,
) -> float | None:
    s = _window_return(strat_nav, w_start, w_end, initial_capital)
    o = _window_return(other_nav, w_start, w_end, initial_capital)
    if s is None or o is None:
        return None
    return s - o


def _wfa_win_rate_vs_index(
    strat_nav: pd.DataFrame,
    index_nav: pd.DataFrame,
    start: str,
    end: str,
    valid_start: str,
    initial_capital: float,
) -> float | None:
    wins = 0
    total = 0
    for _label, w_start, w_end in iter_annual_windows(start, end):
        if w_end < valid_start:
            continue
        edge = _period_edge(strat_nav, index_nav, w_start, w_end, initial_capital)
        if edge is None:
            continue
        total += 1
        if edge > 0:
            wins += 1
    if total == 0:
        return None
    return wins / total


def _max_drawdown_pct(nav_df: pd.DataFrame) -> float | None:
    if nav_df.empty:
        return None
    dd = (nav_df["nav"] / nav_df["nav"].cummax() - 1).min()
    return float(dd * 100)


def _params_from_dict(raw: dict[str, Any]) -> StrategyParams:
    allowed = {f.name for f in StrategyParams.__dataclass_fields__.values()}
    clean = {k: v for k, v in raw.items() if k in allowed and v is not None}
    return StrategyParams(**clean)


def evaluate_params(
    params: dict[str, Any],
    *,
    ctx: BacktestContext,
    start: str,
    end: str,
    valid_start: str,
    initial_capital: float,
    index_nav: pd.DataFrame,
    prefetch_size: int,
) -> TrialMetrics:
    sp = _params_from_dict(params)
    top_n = sp.resolved_top_n()
    rebalance_days = sp.resolved_rebalance_days()

    strat_nav, _, _, _, strat_meta, _ = run_backtest(
        start=start,
        end=end,
        top_n=top_n,
        rebalance_days=rebalance_days,
        initial_capital=initial_capital,
        prefetch_size=prefetch_size,
        ctx=ctx,
        record_details=False,
        verbose=False,
        strategy_params=sp,
    )
    hold_nav, _, _, _, hold_meta, _ = run_backtest(
        start=start,
        end=end,
        top_n=top_n,
        rebalance_days=rebalance_days,
        initial_capital=initial_capital,
        prefetch_size=prefetch_size,
        hold_only=True,
        ctx=ctx,
        record_details=False,
        verbose=False,
        strategy_params=sp,
    )

    train_end = (pd.Timestamp(valid_start) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    valid_edge_index = _period_edge(strat_nav, index_nav, valid_start, end, initial_capital)
    valid_edge_hold = _period_edge(strat_nav, hold_nav, valid_start, end, initial_capital)
    train_edge_index = _period_edge(strat_nav, index_nav, start, train_end, initial_capital)
    train_edge_hold = _period_edge(strat_nav, hold_nav, start, train_end, initial_capital)
    full_edge_index = _period_edge(strat_nav, index_nav, start, end, initial_capital)
    full_edge_hold = _period_edge(strat_nav, hold_nav, start, end, initial_capital)

    wfa_win = _wfa_win_rate_vs_index(strat_nav, index_nav, start, end, valid_start, initial_capital)
    max_dd = _max_drawdown_pct(strat_nav)

    index_ret = None
    if not index_nav.empty:
        index_ret = (float(index_nav["nav"].iloc[-1]) / initial_capital - 1) * 100

    score = 0.0
    if valid_edge_index is not None:
        score += 0.15 * valid_edge_index
    if valid_edge_hold is not None:
        score += 0.15 * valid_edge_hold
    if wfa_win is not None:
        score += 0.10 * (wfa_win * 100)
    if max_dd is not None:
        score -= 0.55 * abs(min(max_dd, 0))
    if strat_meta.get("total_return_pct") is not None:
        score += 0.05 * float(strat_meta["total_return_pct"])

    return TrialMetrics(
        score=score,
        valid_edge_vs_index_pct=valid_edge_index,
        valid_edge_vs_hold_pct=valid_edge_hold,
        train_edge_vs_index_pct=train_edge_index,
        train_edge_vs_hold_pct=train_edge_hold,
        full_edge_vs_index_pct=full_edge_index,
        full_edge_vs_hold_pct=full_edge_hold,
        valid_wfa_win_rate_vs_index=wfa_win,
        strategy_return_pct=strat_meta.get("total_return_pct"),
        hold_return_pct=hold_meta.get("total_return_pct"),
        index_return_pct=index_ret,
        max_drawdown_pct=max_dd,
    )


def run_trial(
    trial_id: int,
    params: dict[str, Any],
    *,
    ctx: BacktestContext,
    start: str,
    end: str,
    valid_start: str,
    initial_capital: float,
    index_nav: pd.DataFrame,
    prefetch_size: int,
    label: str = "",
    task: str = "",
) -> TrialResult:
    t0 = time.perf_counter()
    metrics = evaluate_params(
        params,
        ctx=ctx,
        start=start,
        end=end,
        valid_start=valid_start,
        initial_capital=initial_capital,
        index_nav=index_nav,
        prefetch_size=prefetch_size,
    )
    return TrialResult(
        trial_id=trial_id,
        params=params,
        metrics=metrics,
        elapsed_sec=time.perf_counter() - t0,
        label=label,
        task=task,
    )


def run_screen(
    *,
    ctx: BacktestContext,
    start: str,
    end: str,
    valid_start: str,
    initial_capital: float,
    index_nav: pd.DataFrame,
    prefetch_size: int,
) -> tuple[list[ScreenRow], TrialMetrics]:
    baseline = evaluate_params(
        {},
        ctx=ctx,
        start=start,
        end=end,
        valid_start=valid_start,
        initial_capital=initial_capital,
        index_nav=index_nav,
        prefetch_size=prefetch_size,
    )
    rows: list[ScreenRow] = []
    for spec in SCREEN_SPECS:
        key = spec["key"]
        label = spec["label"]
        for val in spec["values"]:
            trial_params = {key: val}
            m = evaluate_params(
                trial_params,
                ctx=ctx,
                start=start,
                end=end,
                valid_start=valid_start,
                initial_capital=initial_capital,
                index_nav=index_nav,
                prefetch_size=prefetch_size,
            )
            edge_idx = None
            edge_hold = None
            if m.valid_edge_vs_index_pct is not None and baseline.valid_edge_vs_index_pct is not None:
                edge_idx = m.valid_edge_vs_index_pct - baseline.valid_edge_vs_index_pct
            if m.valid_edge_vs_hold_pct is not None and baseline.valid_edge_vs_hold_pct is not None:
                edge_hold = m.valid_edge_vs_hold_pct - baseline.valid_edge_vs_hold_pct
            score_delta = m.score - baseline.score
            max_edge = max(abs(edge_idx or 0), abs(edge_hold or 0))
            verdict = "keep" if max_edge >= SCREEN_EDGE_THRESHOLD else "drop"
            rows.append(
                ScreenRow(
                    param_key=key,
                    label=label,
                    value=val,
                    edge_delta_vs_index_pct=edge_idx,
                    edge_delta_vs_hold_pct=edge_hold,
                    score_delta=score_delta,
                    verdict=verdict,
                )
            )
            print(
                f"  {label}={val}: Δ指数 {_fmt_pct(edge_idx)} Δ持有 {_fmt_pct(edge_hold)} "
                f"→ {verdict}"
            )
    return rows, baseline


def active_grid_space(screen_rows: list[ScreenRow] | None) -> dict[str, list[Any]]:
    if not screen_rows:
        return COARSE_GRID.copy()
    kept_keys = {r.param_key for r in screen_rows if r.verdict == "keep"}
    space = {k: v for k, v in COARSE_GRID.items() if k in kept_keys or k not in {s["key"] for s in SCREEN_SPECS}}
    return space or COARSE_GRID.copy()


def iter_grid(space: dict[str, list[Any]]):
    keys = list(space.keys())
    for combo in itertools.product(*(space[k] for k in keys)):
        yield dict(zip(keys, combo))


def search_grid(
    space: dict[str, list[Any]],
    evaluate: Callable[[int, dict], TrialResult],
) -> list[TrialResult]:
    results: list[TrialResult] = []
    combos = list(iter_grid(space))
    for i, params in enumerate(combos, 1):
        results.append(evaluate(i, params))
        if i % 5 == 0 or i == len(combos):
            print(f"  网格 {i}/{len(combos)} 完成…")
    return results


def search_random(
    space: dict[str, list[Any]],
    trials: int,
    evaluate: Callable[[int, dict], TrialResult],
    seed: int,
) -> list[TrialResult]:
    rng = random.Random(seed)
    results: list[TrialResult] = []
    keys = list(space.keys())
    for i in range(1, trials + 1):
        params = {k: rng.choice(space[k]) for k in keys}
        results.append(evaluate(i, params))
    return results


def search_bayesian(
    base_params: dict[str, Any],
    trials: int,
    evaluate: Callable[[int, dict], TrialResult],
    seed: int,
) -> list[TrialResult]:
    try:
        import optuna
    except ImportError:
        print("未安装 optuna，改用随机搜索代替贝叶斯优化")
        random_space = {k: [base_params.get(k, (lo + hi) / 2)] for k, (lo, hi) in BAYESIAN_BOUNDS.items()}
        for k, (lo, hi) in BAYESIAN_BOUNDS.items():
            random_space[k] = [lo, (lo + hi) / 2, hi]
        return search_random(random_space, trials, evaluate, seed)

    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def objective(trial: optuna.Trial) -> float:
        params = dict(base_params)
        for key, (lo, hi) in BAYESIAN_BOUNDS.items():
            if key in ("min_roe_pct",):
                params[key] = trial.suggest_float(key, lo, hi, step=1.0)
            elif key == "max_industry_weight":
                params[key] = trial.suggest_float(key, lo, hi, step=0.01)
            else:
                params[key] = trial.suggest_float(key, lo, hi)
        result = evaluate(trial.number + 1, params)
        return result.metrics.score

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=seed))
    study.optimize(objective, n_trials=trials, show_progress_bar=False)
    results = [t.user_attrs["result"] for t in study.trials if "result" in t.user_attrs]
    return results


def _wrap_evaluate(
    ctx: BacktestContext,
    start: str,
    end: str,
    valid_start: str,
    initial_capital: float,
    index_nav: pd.DataFrame,
    prefetch_size: int,
    task: str,
    store: list[TrialResult],
) -> Callable[[int, dict], TrialResult]:
    def _eval(trial_id: int, params: dict) -> TrialResult:
        result = run_trial(
            trial_id,
            params,
            ctx=ctx,
            start=start,
            end=end,
            valid_start=valid_start,
            initial_capital=initial_capital,
            index_nav=index_nav,
            prefetch_size=prefetch_size,
            label=StrategyParams().merge(params).summary(),
            task=task,
        )
        store.append(result)
        return result

    return _eval


def print_top(results: list[TrialResult], top: int, valid_start: str) -> None:
    ranked = sorted(results, key=lambda r: r.metrics.score, reverse=True)[:top]
    print(f"\n=== Top {len(ranked)}（验证段自 {valid_start}）===")
    print(
        f"{'#':>3} {'得分':>7} {'验证vs指数':>10} {'验证vs持有':>10} "
        f"{'WFA胜率':>8} {'全段收益':>9} 参数"
    )
    print("-" * 100)
    for i, r in enumerate(ranked, 1):
        m = r.metrics
        wfa = "—" if m.valid_wfa_win_rate_vs_index is None else f"{m.valid_wfa_win_rate_vs_index * 100:.0f}%"
        print(
            f"{i:>3} {m.score:>7.2f} {_fmt_pct(m.valid_edge_vs_index_pct):>10} "
            f"{_fmt_pct(m.valid_edge_vs_hold_pct):>10} {wfa:>8} "
            f"{_fmt_pct(m.strategy_return_pct):>9} {r.label[:50]}"
        )


def format_report(
    *,
    meta: dict,
    baseline: TrialMetrics | None,
    screen_rows: list[ScreenRow],
    grid_results: list[TrialResult],
    bayes_results: list[TrialResult],
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 红利低波轮动 — 参数优化报告",
        "",
        f"> 生成时间：{now}",
        f"> 区间：{meta['start']} ~ {meta['end']}",
        f"> 训练段：{meta['start']} ~ {(pd.Timestamp(meta['valid_start']) - pd.Timedelta(days=1)).date()}",
        f"> 验证段：{meta['valid_start']} ~ {meta['end']}",
        "",
        "## 目标函数",
        "",
        "score = 0.4×验证超额(指数) + 0.3×验证超额(持有) + 0.2×验证WFA胜率 − 0.1×|最大回撤|",
        "",
    ]
    if baseline:
        lines.extend(
            [
                "## 基线（默认参数）",
                "",
                f"- 得分：**{baseline.score:.2f}**",
                f"- 验证超额 vs 指数：{_fmt_pct(baseline.valid_edge_vs_index_pct)}",
                f"- 验证超额 vs 持有：{_fmt_pct(baseline.valid_edge_vs_hold_pct)}",
                f"- 全段收益：策略 {_fmt_pct(baseline.strategy_return_pct)} / 指数 {_fmt_pct(baseline.index_return_pct)}",
                "",
            ]
        )
    if screen_rows:
        kept = sum(1 for r in screen_rows if r.verdict == "keep")
        lines.extend(
            [
                "## 单参数筛选（screen）",
                "",
                f"有影响参数：**{kept}** / {len(set(r.param_key for r in screen_rows))} 项",
                "",
                "| 参数 | 取值 | Δ验证超额(指数) | Δ验证超额(持有) | 判定 |",
                "|------|------|-----------------|-----------------|------|",
            ]
        )
        for r in sorted(screen_rows, key=lambda x: abs(x.edge_delta_vs_index_pct or 0), reverse=True):
            lines.append(
                f"| {r.label} | {r.value} | {_fmt_pct(r.edge_delta_vs_index_pct)} | "
                f"{_fmt_pct(r.edge_delta_vs_hold_pct)} | {r.verdict} |"
            )
        lines.append("")
    if grid_results:
        best = max(grid_results, key=lambda r: r.metrics.score)
        lines.extend(
            [
                "## 粗网格（top_n × rebalance × sell_mult）",
                "",
                f"- 试验次数：**{len(grid_results)}**",
                f"- 最优得分：**{best.metrics.score:.2f}**",
                f"- 最优参数：`{best.label}`",
                f"- 验证超额 vs 指数：{_fmt_pct(best.metrics.valid_edge_vs_index_pct)}",
                "",
                "| 排名 | 得分 | 验证vs指数 | 验证vs持有 | 全段收益 | 参数 |",
                "|------|------|------------|------------|----------|------|",
            ]
        )
        for i, r in enumerate(sorted(grid_results, key=lambda x: x.metrics.score, reverse=True)[:10], 1):
            m = r.metrics
            lines.append(
                f"| {i} | {m.score:.2f} | {_fmt_pct(m.valid_edge_vs_index_pct)} | "
                f"{_fmt_pct(m.valid_edge_vs_hold_pct)} | {_fmt_pct(m.strategy_return_pct)} | {r.label} |"
            )
        lines.append("")
    if bayes_results:
        best = max(bayes_results, key=lambda r: r.metrics.score)
        lines.extend(
            [
                "## 贝叶斯精调（连续参数）",
                "",
                f"- 试验次数：**{len(bayes_results)}**",
                f"- 最优得分：**{best.metrics.score:.2f}**",
                f"- 最优参数：`{best.label}`",
                f"- 验证超额 vs 指数：{_fmt_pct(best.metrics.valid_edge_vs_index_pct)}",
                "",
            ]
        )
    all_results = grid_results + bayes_results
    if all_results:
        overall = max(all_results, key=lambda r: r.metrics.score)
        lines.extend(
            [
                "## 综合最优",
                "",
                f"- 参数：`{overall.label}`",
                f"- 得分：**{overall.metrics.score:.2f}**",
                f"- 验证超额 vs 指数：**{_fmt_pct(overall.metrics.valid_edge_vs_index_pct)}**",
                f"- 验证超额 vs 持有：**{_fmt_pct(overall.metrics.valid_edge_vs_hold_pct)}**",
                f"- 全段：策略 {_fmt_pct(overall.metrics.strategy_return_pct)} / "
                f"指数 {_fmt_pct(overall.metrics.index_return_pct)} / "
                f"持有 {_fmt_pct(overall.metrics.hold_return_pct)}",
                "",
            ]
        )
    return "\n".join(lines)


def save_outputs(
    out_dir: Path,
    *,
    meta: dict,
    baseline: TrialMetrics | None,
    screen_rows: list[ScreenRow],
    grid_results: list[TrialResult],
    bayes_results: list[TrialResult],
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{OUTPUT_STEM}.md"
    json_path = out_dir / f"{OUTPUT_STEM}.json"
    md_path.write_text(
        format_report(
            meta=meta,
            baseline=baseline,
            screen_rows=screen_rows,
            grid_results=grid_results,
            bayes_results=bayes_results,
        ),
        encoding="utf-8",
    )
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "meta": meta,
        "baseline": asdict(baseline) if baseline else None,
        "screen_rows": [asdict(r) for r in screen_rows],
        "grid_results": [
            {"trial_id": r.trial_id, "params": r.params, "metrics": asdict(r.metrics), "label": r.label}
            for r in grid_results
        ],
        "bayes_results": [
            {"trial_id": r.trial_id, "params": r.params, "metrics": asdict(r.metrics), "label": r.label}
            for r in bayes_results
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"md": md_path, "json": json_path}


def main(argv: list[str] | None = None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="红利低波轮动参数优化")
    parser.add_argument(
        "--task",
        choices=["screen", "grid", "atr_grid", "bayesian", "all"],
        default="all",
    )
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--valid-start", default=DEFAULT_VALID_START)
    parser.add_argument("--capital", type=float, default=BACKTEST_INITIAL_CAPITAL)
    parser.add_argument("--prefetch", type=int, default=BACKTEST_PREFETCH_SIZE)
    parser.add_argument("--trials", type=int, default=50, help="贝叶斯试验次数")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("-o", "--output-dir", default=None)
    args = parser.parse_args(argv)

    start = args.start or default_start_years(args.years)
    end = args.end or pd.Timestamp.today().strftime("%Y-%m-%d")
    if pd.Timestamp(args.valid_start) <= pd.Timestamp(start):
        print("valid-start 必须晚于 start")
        return 1

    out_dir = Path(args.output_dir) if args.output_dir else BACKTEST_OUTPUT_DIR
    t0 = time.time()

    print(f"预加载数据 {start} ~ {end}…")
    ctx = prepare_backtest_context(
        start,
        end,
        prefetch_size=args.prefetch,
        rebalance_days=10,
        verbose=True,
    )
    index_nav, _ = load_index_benchmark_nav("H30269", start, end, args.capital)

    baseline: TrialMetrics | None = None
    screen_rows: list[ScreenRow] = []
    grid_results: list[TrialResult] = []
    bayes_results: list[TrialResult] = []
    best_grid_params: dict[str, Any] = {}

    if args.task in ("screen", "all"):
        print(f"\n=== 单参数筛选 | 验证段 {args.valid_start} 起 ===")
        screen_rows, baseline = run_screen(
            ctx=ctx,
            start=start,
            end=end,
            valid_start=args.valid_start,
            initial_capital=args.capital,
            index_nav=index_nav,
            prefetch_size=args.prefetch,
        )

    if args.task in ("grid", "all"):
        space = active_grid_space(screen_rows if screen_rows else None)
        n = 1
        for vals in space.values():
            n *= len(vals)
        print(f"\n=== 粗网格 {n} 组 ===")
        grid_store: list[TrialResult] = []
        evaluator = _wrap_evaluate(
            ctx, start, end, args.valid_start, args.capital, index_nav, args.prefetch, "grid", grid_store
        )
        grid_results = search_grid(space, evaluator)
        if grid_results:
            best = max(grid_results, key=lambda r: r.metrics.score)
            best_grid_params = dict(best.params)
            print_top(grid_results, 5, args.valid_start)

    atr_results: list[TrialResult] = []
    if args.task in ("atr_grid", "all"):
        n_atr = len(ATR_GRID["stop_atr_multiplier"])
        print(f"\n=== ATR 止损乘数网格 {n_atr} 组 ===")
        atr_store: list[TrialResult] = []
        evaluator = _wrap_evaluate(
            ctx, start, end, args.valid_start, args.capital, index_nav, args.prefetch, "atr_grid", atr_store
        )
        for i, mult in enumerate(ATR_GRID["stop_atr_multiplier"], 1):
            result = evaluator(i, {"stop_atr_multiplier": mult})
            atr_results.append(result)
            if i % 3 == 0 or i == n_atr:
                print(f"  ATR网格 {i}/{n_atr} 完成…")
        if atr_results:
            print_top(atr_results, 5, args.valid_start)

    if args.task in ("bayesian", "all"):
        base = best_grid_params or {"top_n": 10, "rebalance_days": 20, "sell_rank_multiplier": 2.0}
        print(f"\n=== 贝叶斯精调 {args.trials} 次（基于 {base}）===")
        bayes_store: list[TrialResult] = []

        def bayes_eval(trial_id: int, params: dict) -> TrialResult:
            merged = {**base, **params}
            result = run_trial(
                trial_id,
                merged,
                ctx=ctx,
                start=start,
                end=end,
                valid_start=args.valid_start,
                initial_capital=args.capital,
                index_nav=index_nav,
                prefetch_size=args.prefetch,
                label=_params_from_dict(merged).summary(),
                task="bayesian",
            )
            bayes_store.append(result)
            if trial_id % 10 == 0:
                print(f"  贝叶斯 {trial_id}/{args.trials} 当前最优 {max(bayes_store, key=lambda r: r.metrics.score).metrics.score:.2f}")
            return result

        try:
            import optuna

            optuna.logging.set_verbosity(optuna.logging.WARNING)

            def objective(trial: optuna.Trial) -> float:
                params = dict(base)
                for key, (lo, hi) in BAYESIAN_BOUNDS.items():
                    if key == "min_roe_pct":
                        params[key] = trial.suggest_float(key, lo, hi, step=1.0)
                    elif key == "max_industry_weight":
                        params[key] = trial.suggest_float(key, lo, hi, step=0.01)
                    else:
                        params[key] = trial.suggest_float(key, lo, hi)
                result = bayes_eval(trial.number + 1, params)
                trial.set_user_attr("result_id", result.trial_id)
                return result.metrics.score

            study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=args.seed))
            study.optimize(objective, n_trials=args.trials, show_progress_bar=False)
            bayes_results = bayes_store
        except ImportError:
            print("未安装 optuna，使用随机搜索")
            random_space = {k: [lo, (lo + hi) / 2, hi] for k, (lo, hi) in BAYESIAN_BOUNDS.items()}
            evaluator = _wrap_evaluate(
                ctx, start, end, args.valid_start, args.capital, index_nav, args.prefetch, "bayesian", bayes_store
            )
            for i, combo in enumerate(iter_grid({**random_space, **{k: [base[k]] for k in base if k not in random_space}}), 1):
                if i > args.trials:
                    break
                evaluator(i, combo)
            bayes_results = bayes_store

        if bayes_results:
            print_top(bayes_results, 5, args.valid_start)

    if baseline is None and args.task == "bayesian":
        baseline = evaluate_params(
            {},
            ctx=ctx,
            start=start,
            end=end,
            valid_start=args.valid_start,
            initial_capital=args.capital,
            index_nav=index_nav,
            prefetch_size=args.prefetch,
        )

    meta = {
        "start": start,
        "end": end,
        "valid_start": args.valid_start,
        "task": args.task,
        "elapsed_sec": time.time() - t0,
    }
    paths = save_outputs(
        out_dir,
        meta=meta,
        baseline=baseline,
        screen_rows=screen_rows,
        grid_results=grid_results,
        bayes_results=bayes_results,
    )
    print(f"\n总耗时 {meta['elapsed_sec']:.0f} 秒")
    print(f"报告：{paths['md']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
