# -*- coding: utf-8 -*-
"""增强因子阈值优化：固定 1 月调仓，网格 + 贝叶斯搜索。

用法
----
    python scripts/optimize_enhanced_factors.py --task all --years 10 --end 2025-08-01
    python scripts/optimize_enhanced_factors.py --task grid
    python scripts/optimize_enhanced_factors.py --task bayesian --trials 40
"""

from __future__ import annotations

import argparse
import importlib
import itertools
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# 固定：1 月调仓 + 增强因子全开
FIXED_ENV: dict[str, str] = {
    "DLV_INDEX_ANNUAL_REBALANCE_TIMING": "january",
    "DLV_BACKTEST_KLINE_FQ": "qfq",
    "DLV_SUSTAINABLE_DIVIDEND_ENABLED": "true",
    "DLV_YIELD_SPREAD_PERCENTILE_ENABLED": "true",
    "DLV_PROFIT_MOMENTUM_FILTER_ENABLED": "true",
    "DLV_PROFIT_STABILITY_FILTER_ENABLED": "true",
    "DLV_DIVIDEND_COVERAGE_FILTER_ENABLED": "true",
    "DLV_BETA_BALANCE_ENABLED": "true",
}

PARAM_ENV: dict[str, str] = {
    "yield_spread_trap": "DLV_YIELD_SPREAD_PERCENTILE_TRAP",
    "profit_momentum_min_qoq": "DLV_PROFIT_MOMENTUM_MIN_QOQ_POSITIVE",
    "max_profit_cv": "DLV_MAX_PROFIT_CV",
    "min_dividend_coverage": "DLV_MIN_DIVIDEND_COVERAGE",
    "beta_low_threshold": "DLV_BETA_LOW_THRESHOLD",
    "beta_min_low_frac": "DLV_BETA_MIN_LOW_FRAC",
    "beta_max_high_frac": "DLV_BETA_MAX_HIGH_FRAC",
}

DEFAULT_PARAMS: dict[str, float | int] = {
    "yield_spread_trap": 95.0,
    "profit_momentum_min_qoq": 1,
    "max_profit_cv": 0.7,
    "min_dividend_coverage": 1.0,
    "beta_low_threshold": 0.8,
    "beta_min_low_frac": 0.30,
    "beta_max_high_frac": 0.70,
}

COARSE_GRID: dict[str, list[Any]] = {
    "yield_spread_trap": [85.0, 90.0, 92.0, 95.0],
    "profit_momentum_min_qoq": [1, 2],
    "max_profit_cv": [0.50, 0.60, 0.70],
}

BAYES_BOUNDS: dict[str, tuple[float, float]] = {
    "yield_spread_trap": (85.0, 98.0),
    "profit_momentum_min_qoq": (1.0, 2.0),
    "max_profit_cv": (0.45, 0.75),
    "min_dividend_coverage": (1.0, 2.0),
    "beta_low_threshold": (0.65, 0.95),
    "beta_min_low_frac": (0.15, 0.35),
    "beta_max_high_frac": (0.65, 0.85),
}

RELOAD_MODULES = (
    "dividend_lowvol_rotation.config",
    "dividend_lowvol_rotation.enhanced_factors",
    "dividend_lowvol_rotation.index_portfolio",
    "dividend_lowvol_rotation.industry_caps",
    "dividend_lowvol_rotation.rebalance_schedule",
    "dividend_lowvol_rotation.scoring",
    "dividend_lowvol_rotation.backtest",
)

DEFAULT_VALID_START = "2021-01-01"
OUTPUT_STEM = "optimize_enhanced_factors"
MIN_TRADES_FOR_SCORE = 50


def _compute_score(
    *,
    total_ret: float | None,
    max_dd: float | None,
    trades: int,
    sharpe: float | None = None,
) -> float:
    """收益最大化 + 回撤最小化；成交不足重罚。"""
    if trades < MIN_TRADES_FOR_SCORE:
        return -500.0 + trades
    ret = float(total_ret or 0.0)
    dd = abs(min(float(max_dd or 0.0), 0.0))
    calmar = ret / dd if dd > 1e-6 else ret
    score = 0.45 * ret - 0.40 * dd + 0.15 * min(calmar, 8.0)
    if sharpe is not None and sharpe > 0:
        score += 0.05 * min(float(sharpe), 2.0) * 10
    return score


@dataclass
class TrialMetrics:
    score: float
    valid_edge_vs_index_pct: float | None
    valid_edge_vs_hold_pct: float | None
    strategy_return_pct: float | None
    max_drawdown_pct: float | None
    trades: int
    wfa_win_rate: float | None


@dataclass
class TrialResult:
    trial_id: int
    params: dict[str, Any]
    metrics: TrialMetrics
    elapsed_sec: float
    task: str


def _reload_modules() -> None:
    from dividend_lowvol_rotation import dynamic_params as dp

    dp._fetch_bond_yield_pct.cache_clear()
    dp._bond_yield_history_series.cache_clear()
    for name in RELOAD_MODULES:
        importlib.reload(importlib.import_module(name))


def _apply_params(params: dict[str, Any]) -> None:
    merged = {**DEFAULT_PARAMS, **params}
    for key in PARAM_ENV:
        os.environ.pop(PARAM_ENV[key], None)
    for key, val in FIXED_ENV.items():
        os.environ[key] = val
    for key, env_key in PARAM_ENV.items():
        val = merged.get(key)
        if val is not None:
            if key == "profit_momentum_min_qoq":
                os.environ[env_key] = str(int(round(float(val))))
            else:
                os.environ[env_key] = str(val)
    _reload_modules()


def _params_label(params: dict[str, Any]) -> str:
    parts = []
    for k in sorted(params):
        v = params[k]
        if isinstance(v, float) and v == int(v):
            v = int(v)
        parts.append(f"{k}={v}")
    return ", ".join(parts)


def _period_edge(strat_nav, other_nav, w_start, w_end, capital) -> float | None:
    from dividend_lowvol_rotation.backtest_validate import _window_return

    s = _window_return(strat_nav, w_start, w_end, capital)
    o = _window_return(other_nav, w_start, w_end, capital)
    if s is None or o is None:
        return None
    return s - o


def _wfa_win_rate(strat_nav, index_nav, start, end, valid_start, capital) -> float | None:
    from dividend_lowvol_rotation.backtest_validate import iter_annual_windows

    wins = total = 0
    for _, w_start, w_end in iter_annual_windows(start, end):
        if w_end < valid_start:
            continue
        edge = _period_edge(strat_nav, index_nav, w_start, w_end, capital)
        if edge is None:
            continue
        total += 1
        if edge > 0:
            wins += 1
    return wins / total if total else None


def evaluate_trial(
    params: dict[str, Any],
    *,
    start: str,
    end: str,
    valid_start: str,
    capital: float,
    index_nav: pd.DataFrame,
    include_hold: bool = True,
    verbose: bool = False,
    ctx=None,
) -> TrialMetrics:
    _apply_params(params)

    from dividend_lowvol_rotation.backtest import prepare_backtest_context, run_backtest
    from dividend_lowvol_rotation.config import BACKTEST_REBALANCE_MODE

    if ctx is None:
        ctx = prepare_backtest_context(start, end, verbose=verbose)
    # panel 按 (日期, prefetch, 因子参数) 缓存；trial 间参数不同自动 miss，相同则复用

    strat_nav, trades_df, _, _, meta, _ = run_backtest(
        start=start,
        end=end,
        ctx=ctx,
        verbose=False,
        record_details=False,
        rebalance_mode=BACKTEST_REBALANCE_MODE,
    )
    hold_nav = strat_nav
    if include_hold:
        hold_nav, _, _, _, _, _ = run_backtest(
            start=start,
            end=end,
            ctx=ctx,
            verbose=False,
            record_details=False,
            hold_only=True,
            rebalance_mode=BACKTEST_REBALANCE_MODE,
        )

    valid_edge_idx = _period_edge(strat_nav, index_nav, valid_start, end, capital)
    valid_edge_hold = _period_edge(strat_nav, hold_nav, valid_start, end, capital)
    wfa_win = _wfa_win_rate(strat_nav, index_nav, start, end, valid_start, capital)
    trades = len(trades_df) if trades_df is not None and not trades_df.empty else 0
    total_ret = meta.get("total_return_pct")
    max_dd = meta.get("max_drawdown_pct")
    sharpe = meta.get("sharpe")

    score = _compute_score(
        total_ret=total_ret,
        max_dd=max_dd,
        trades=trades,
        sharpe=sharpe,
    )

    return TrialMetrics(
        score=score,
        valid_edge_vs_index_pct=valid_edge_idx,
        valid_edge_vs_hold_pct=valid_edge_hold,
        strategy_return_pct=total_ret,
        max_drawdown_pct=max_dd,
        trades=trades,
        wfa_win_rate=wfa_win,
    )


def run_trial(
    trial_id: int,
    params: dict[str, Any],
    *,
    start: str,
    end: str,
    valid_start: str,
    capital: float,
    index_nav: pd.DataFrame,
    task: str,
    include_hold: bool = True,
    ctx=None,
) -> TrialResult:
    t0 = time.perf_counter()
    metrics = evaluate_trial(
        params,
        start=start,
        end=end,
        valid_start=valid_start,
        capital=capital,
        index_nav=index_nav,
        include_hold=include_hold,
        ctx=ctx,
    )
    return TrialResult(
        trial_id=trial_id,
        params=params,
        metrics=metrics,
        elapsed_sec=time.perf_counter() - t0,
        task=task,
    )


def iter_grid(space: dict[str, list[Any]]):
    keys = list(space.keys())
    for combo in itertools.product(*(space[k] for k in keys)):
        yield dict(zip(keys, combo))


def search_grid(space, evaluator) -> list[TrialResult]:
    combos = list(iter_grid(space))
    results = []
    for i, p in enumerate(combos, 1):
        results.append(evaluator(i, p))
        if i % 5 == 0 or i == len(combos):
            best = max(results, key=lambda r: r.metrics.score)
            print(
                f"  网格 {i}/{len(combos)} 当前最优 score={best.metrics.score:.2f} "
                f"收益={best.metrics.strategy_return_pct}% 回撤={best.metrics.max_drawdown_pct}% "
                f"成交={best.metrics.trades}",
                flush=True,
            )
    return results


def search_bayesian(
    base_params: dict[str, Any],
    trials: int,
    evaluator,
    seed: int,
) -> list[TrialResult]:
    try:
        import optuna

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        results: list[TrialResult] = []

        def objective(trial: optuna.Trial) -> float:
            p = dict(base_params)
            p["yield_spread_trap"] = trial.suggest_float("yield_spread_trap", *BAYES_BOUNDS["yield_spread_trap"])
            p["profit_momentum_min_qoq"] = trial.suggest_int(
                "profit_momentum_min_qoq",
                int(BAYES_BOUNDS["profit_momentum_min_qoq"][0]),
                int(BAYES_BOUNDS["profit_momentum_min_qoq"][1]),
            )
            p["max_profit_cv"] = trial.suggest_float("max_profit_cv", *BAYES_BOUNDS["max_profit_cv"])
            p["min_dividend_coverage"] = trial.suggest_float(
                "min_dividend_coverage", *BAYES_BOUNDS["min_dividend_coverage"]
            )
            p["beta_low_threshold"] = trial.suggest_float(
                "beta_low_threshold", *BAYES_BOUNDS["beta_low_threshold"]
            )
            p["beta_min_low_frac"] = trial.suggest_float("beta_min_low_frac", *BAYES_BOUNDS["beta_min_low_frac"])
            p["beta_max_high_frac"] = trial.suggest_float("beta_max_high_frac", *BAYES_BOUNDS["beta_max_high_frac"])
            r = evaluator(trial.number + 1, p)
            results.append(r)
            return r.metrics.score

        study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=seed))
        study.optimize(objective, n_trials=trials, show_progress_bar=False)
        return results
    except ImportError:
        print("未安装 optuna，跳过贝叶斯阶段")
        return []


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    return f"{float(v):+.2f}%"


def format_report(meta, baseline, grid_results, bayes_results) -> str:
    lines = [
        "# 增强因子阈值优化",
        "",
        f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"> 区间：{meta['start']} ~ {meta['end']}",
        f"> 验证段：{meta['valid_start']} ~ {meta['end']}",
        "> **固定**：1 月调仓 + 增强因子全开",
        "",
        "## 目标函数",
        "",
        f"score = 0.45×全段收益 − 0.40×|最大回撤| + 0.15×min(Calmar,8) + Sharpe加成；"
        f"成交<{MIN_TRADES_FOR_SCORE} 笔重罚",
        "",
    ]
    if baseline:
        lines += [
            "## 默认阈值基线",
            "",
            f"- 得分：**{baseline.score:.2f}**",
            f"- 验证超额 vs 指数：{_fmt_pct(baseline.valid_edge_vs_index_pct)}",
            f"- 全段收益：{_fmt_pct(baseline.strategy_return_pct)}",
            f"- 最大回撤：{_fmt_pct(baseline.max_drawdown_pct)}",
            f"- 成交：**{baseline.trades}** 笔",
            "",
        ]
    if grid_results:
        best = max(grid_results, key=lambda r: r.metrics.score)
        lines += [
            "## 网格搜索（利差陷阱 × 盈利动量 × 盈利稳定性 = 24 组）",
            "",
            f"- 最优得分：**{best.metrics.score:.2f}**",
            f"- 最优参数：`{_params_label(best.params)}`",
            f"- 验证超额：{_fmt_pct(best.metrics.valid_edge_vs_index_pct)}",
            f"- 全段收益：{_fmt_pct(best.metrics.strategy_return_pct)}",
            f"- 最大回撤：{_fmt_pct(best.metrics.max_drawdown_pct)}",
            f"- 成交：{best.metrics.trades} 笔",
            "",
            "| # | 得分 | 验证vs指数 | 全段收益 | 回撤 | 成交 | 参数 |",
            "|---|------|------------|----------|------|------|------|",
        ]
        for i, r in enumerate(sorted(grid_results, key=lambda x: x.metrics.score, reverse=True)[:15], 1):
            m = r.metrics
            lines.append(
                f"| {i} | {m.score:.2f} | {_fmt_pct(m.valid_edge_vs_index_pct)} | "
                f"{_fmt_pct(m.strategy_return_pct)} | {_fmt_pct(m.max_drawdown_pct)} | "
                f"{m.trades} | {_params_label(r.params)} |"
            )
        lines.append("")
    if bayes_results:
        best = max(bayes_results, key=lambda r: r.metrics.score)
        lines += [
            "## 贝叶斯精调（7 因子连续搜索）",
            "",
            f"- 试验次数：**{len(bayes_results)}**",
            f"- 最优得分：**{best.metrics.score:.2f}**",
            f"- 最优参数：`{_params_label(best.params)}`",
            f"- 验证超额：{_fmt_pct(best.metrics.valid_edge_vs_index_pct)}",
            f"- 全段收益：{_fmt_pct(best.metrics.strategy_return_pct)}",
            "",
            "### 推荐环境变量",
            "",
            "```bash",
        ]
        for k, env in PARAM_ENV.items():
            if k in best.params:
                lines.append(f"export {env}={best.params[k]}")
        lines += ["export DLV_INDEX_ANNUAL_REBALANCE_TIMING=january", "```", ""]
    all_r = grid_results + bayes_results
    if all_r:
        overall = max(all_r, key=lambda r: r.metrics.score)
        valid = [r for r in all_r if r.metrics.trades >= MIN_TRADES_FOR_SCORE]
        best_ret = max(valid, key=lambda r: r.metrics.strategy_return_pct or -1e9) if valid else None
        best_dd = min(valid, key=lambda r: abs(min(float(r.metrics.max_drawdown_pct or 0), 0))) if valid else None
        lines += [
            "## 综合最优（收益-回撤平衡）",
            "",
            f"- `{_params_label(overall.params)}`",
            f"- 得分 **{overall.metrics.score:.2f}** · 全段 {_fmt_pct(overall.metrics.strategy_return_pct)} · "
            f"回撤 {_fmt_pct(overall.metrics.max_drawdown_pct)} · 成交 {overall.metrics.trades}",
            "",
        ]
        if best_ret and best_ret is not overall:
            lines += [
                "## 最高收益",
                "",
                f"- `{_params_label(best_ret.params)}` · {_fmt_pct(best_ret.metrics.strategy_return_pct)} · "
                f"回撤 {_fmt_pct(best_ret.metrics.max_drawdown_pct)}",
                "",
            ]
        if best_dd and best_dd not in (overall, best_ret):
            lines += [
                "## 最小回撤",
                "",
                f"- `{_params_label(best_dd.params)}` · {_fmt_pct(best_dd.metrics.strategy_return_pct)} · "
                f"回撤 {_fmt_pct(best_dd.metrics.max_drawdown_pct)}",
                "",
            ]
    return "\n".join(lines)


def main(argv=None) -> int:
    from dividend_lowvol_rotation.backtest import default_start_years, prepare_backtest_context
    from dividend_lowvol_rotation.backtest_validate import load_index_benchmark_nav
    from dividend_lowvol_rotation.config import BACKTEST_INITIAL_CAPITAL, BACKTEST_OUTPUT_DIR
    from market_data import configure_stdout_utf8

    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="增强因子阈值优化（1月调仓固定）")
    parser.add_argument("--task", choices=["grid", "bayesian", "all"], default="all")
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default="2025-08-01")
    parser.add_argument("--valid-start", default=DEFAULT_VALID_START)
    parser.add_argument("--capital", type=float, default=BACKTEST_INITIAL_CAPITAL)
    parser.add_argument("--trials", type=int, default=40)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    start = args.start or default_start_years(args.years)
    end = args.end
    out_dir = BACKTEST_OUTPUT_DIR
    t0 = time.time()

    for k, v in FIXED_ENV.items():
        os.environ[k] = v
    _apply_params({})

    print(f"预加载指数基准与 K 线 {start} ~ {end}…", flush=True)
    index_nav, _ = load_index_benchmark_nav("H30269", start, end, args.capital)
    shared_ctx = prepare_backtest_context(start, end, verbose=True)

    print("\n评估默认阈值基线…", flush=True)
    baseline = evaluate_trial(
        {},
        start=start,
        end=end,
        valid_start=args.valid_start,
        capital=args.capital,
        index_nav=index_nav,
        ctx=shared_ctx,
    )
    print(
        f"  基线 score={baseline.score:.2f} 收益={baseline.strategy_return_pct}% "
        f"回撤={baseline.max_drawdown_pct}% 成交={baseline.trades}",
        flush=True,
    )

    grid_results: list[TrialResult] = []
    bayes_results: list[TrialResult] = []
    best_grid: dict[str, Any] = dict(DEFAULT_PARAMS)

    def make_eval(task: str, *, include_hold: bool = True):
        def _eval(trial_id: int, params: dict) -> TrialResult:
            return run_trial(
                trial_id,
                params,
                start=start,
                end=end,
                valid_start=args.valid_start,
                capital=args.capital,
                index_nav=index_nav,
                task=task,
                include_hold=include_hold,
                ctx=shared_ctx,
            )

        return _eval

    if args.task in ("grid", "all"):
        n = 1
        for v in COARSE_GRID.values():
            n *= len(v)
        print(f"\n=== 网格搜索 {n} 组（1月调仓固定）===", flush=True)
        grid_results = search_grid(COARSE_GRID, make_eval("grid", include_hold=False))
        if grid_results:
            best = max(grid_results, key=lambda r: r.metrics.score)
            best_grid = {**DEFAULT_PARAMS, **best.params}

    if args.task in ("bayesian", "all"):
        print(f"\n=== 贝叶斯精调 {args.trials} 次（基于网格最优）===", flush=True)
        bayes_results = search_bayesian(best_grid, args.trials, make_eval("bayesian", include_hold=True), args.seed)

    meta = {
        "start": start,
        "end": end,
        "valid_start": args.valid_start,
        "task": args.task,
        "elapsed_sec": time.time() - t0,
    }
    md = out_dir / f"{OUTPUT_STEM}.md"
    js = out_dir / f"{OUTPUT_STEM}.json"
    md.write_text(format_report(meta, baseline, grid_results, bayes_results), encoding="utf-8")
    js.write_text(
        json.dumps(
            {
                "meta": meta,
                "baseline": asdict(baseline),
                "grid": [{"params": r.params, "metrics": asdict(r.metrics)} for r in grid_results],
                "bayesian": [{"params": r.params, "metrics": asdict(r.metrics)} for r in bayes_results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n总耗时 {meta['elapsed_sec']:.0f}s")
    print(f"报告：{md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
