#!/usr/bin/env python
"""Run the locked nine-item fusion_v2 audit acceptance matrix."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Sequence

import duckdb
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.block_bootstrap import OUTPUT as BLOCK_OUTPUT
from scripts.block_bootstrap import run_block_bootstrap
from scripts.incremental_capital_test import OUTPUT as CAPITAL_OUTPUT
from scripts.incremental_capital_test import run_incremental_capital_test


QFQ_GLOB = ROOT / "data" / "parquet" / "stock_daily_qfq" / "year=*" / "*.parquet"
RAW_GLOB = ROOT / "data" / "parquet" / "stock_daily" / "year=*" / "*.parquet"
OUTPUT = ROOT / "output" / "validation"


def _summary(values: np.ndarray) -> dict[str, float]:
    clean = np.asarray(values, dtype=float)
    clean = clean[np.isfinite(clean)]
    if not len(clean):
        raise ValueError("稳健性模拟没有有效结果")
    return {
        "mean": float(np.mean(clean)),
        "std": float(np.std(clean, ddof=1)) if len(clean) > 1 else 0.0,
        "p05": float(np.quantile(clean, 0.05)),
        "p50": float(np.quantile(clean, 0.50)),
        "p95": float(np.quantile(clean, 0.95)),
    }


def load_returns(symbols: Sequence[str], start: str, end: str) -> pd.DataFrame:
    if not symbols:
        raise ValueError("验收回测没有持仓证券")
    escaped = ",".join(f"'{str(symbol).replace(chr(39), chr(39)*2)}'" for symbol in symbols)
    path = QFQ_GLOB.resolve().as_posix().replace("'", "''")
    raw_path = RAW_GLOB.resolve().as_posix().replace("'", "''")
    with duckdb.connect() as con:
        frame = con.execute(
            f"""
            WITH raw AS (
              SELECT try_cast(trade_date AS DATE) trade_date, symbol, max(close) close_price
              FROM read_parquet('{raw_path}', hive_partitioning=true, union_by_name=true)
              WHERE try_cast(trade_date AS DATE) BETWEEN DATE '{start}' - INTERVAL 20 DAY
                AND DATE '{end}' + INTERVAL 20 DAY AND symbol IN ({escaped})
              GROUP BY trade_date, symbol
            ), qfq AS (
              SELECT try_cast(trade_date AS DATE) trade_date, symbol, max(close) close_price
              FROM read_parquet('{path}', hive_partitioning=true, union_by_name=true)
              WHERE try_cast(trade_date AS DATE) BETWEEN DATE '{start}' - INTERVAL 20 DAY
                AND DATE '{end}' + INTERVAL 20 DAY AND symbol IN ({escaped})
              GROUP BY trade_date, symbol
            )
            SELECT raw.trade_date, raw.symbol, coalesce(qfq.close_price, raw.close_price) close_price
            FROM raw LEFT JOIN qfq USING (trade_date, symbol)
            ORDER BY raw.trade_date, raw.symbol
            """
        ).fetchdf()
    close = frame.pivot(index="trade_date", columns="symbol", values="close_price")
    close.index = pd.to_datetime(close.index)
    return close.sort_index().pct_change(fill_method=None)


def simulated_annual_return(
    returns: np.ndarray,
    event_locations: np.ndarray,
    target_columns: list[np.ndarray],
    *,
    cost_rate: float = 0.0023,
) -> float:
    log_total = 0.0
    observations = 0
    previous: set[int] = set()
    for index, (start, columns) in enumerate(zip(event_locations, target_columns)):
        stop = int(event_locations[index + 1]) if index + 1 < len(event_locations) else len(returns) - 1
        if stop <= start or len(columns) == 0:
            continue
        segment = returns[int(start) + 1 : stop + 1, columns]
        daily = np.nanmean(segment, axis=1)
        daily = np.nan_to_num(daily, nan=0.0, posinf=0.0, neginf=0.0)
        current = set(int(value) for value in columns)
        turnover = 1.0 if not previous else 1.0 - len(current & previous) / max(len(current), 1)
        if len(daily):
            daily[0] -= turnover * cost_rate
        safe = daily > -0.999999
        log_total += float(np.log1p(daily[safe]).sum())
        observations += int(safe.sum())
        previous = current
    return float(math.exp(log_total * 252.0 / observations) - 1.0) if observations else float("nan")


def stress_tests(
    returns: pd.DataFrame,
    selections: dict[str, list[str]],
    *,
    n_iter: int,
) -> tuple[dict[str, float], dict[str, float]]:
    dates = pd.DatetimeIndex(returns.index)
    events = sorted((pd.Timestamp(day), symbols) for day, symbols in selections.items())
    events = [(day, symbols) for day, symbols in events if day in dates]
    locations = np.asarray([dates.get_loc(day) for day, _ in events], dtype=np.int32)
    column_lookup = {str(symbol): index for index, symbol in enumerate(returns.columns)}
    targets = [
        np.asarray([column_lookup[symbol] for symbol in symbols if symbol in column_lookup], dtype=np.int32)
        for _, symbols in events
    ]
    values = returns.to_numpy(dtype=np.float64)
    random_rng = np.random.default_rng(20260827)
    shift_rng = np.random.default_rng(20260828)
    random_results = np.empty(n_iter, dtype=float)
    shift_results = np.empty(n_iter, dtype=float)
    for iteration in range(n_iter):
        dropped = []
        for columns in targets:
            keep = max(1, int(round(len(columns) * 0.8)))
            dropped.append(np.sort(random_rng.choice(columns, size=keep, replace=False)))
        random_results[iteration] = simulated_annual_return(values, locations, dropped)
        shifted = np.clip(locations + shift_rng.integers(-13, 14, size=len(locations)), 0, len(dates) - 2)
        shifted = np.maximum.accumulate(shifted)
        for index in range(1, len(shifted)):
            shifted[index] = max(shifted[index], shifted[index - 1] + 1)
        shifted = np.minimum(
            shifted,
            np.arange(len(shifted)) + len(dates) - len(shifted) - 1,
        )
        shift_results[iteration] = simulated_annual_return(values, shifted, targets)
    return _summary(random_results), _summary(shift_results)


def _plot_main_vs_control(main_nav: pd.Series, output: Path) -> None:
    control_path = ROOT / "output" / "ablation" / "nav_series_fusion_v2_control.csv"
    if not control_path.is_file():
        return
    control = pd.read_csv(control_path, index_col=0, parse_dates=True)["full"].astype(float)
    frame = pd.concat({"主配置": main_nav, "三因子固定对照": control}, axis=1).dropna()
    normalized = frame.div(frame.iloc[0])
    for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")):
        if path.is_file():
            font_manager.fontManager.addfont(str(path))
            plt.rcParams["font.sans-serif"] = [
                font_manager.FontProperties(fname=str(path)).get_name()
            ]
            break
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(12, 6))
    normalized.plot(ax=ax, linewidth=1.6)
    ax.set_title("fusion_v2 审计整改：主配置与固定对照")
    ax.set_xlabel("日期")
    ax.set_ylabel("净值（起点=1）")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(output, dpi=180)
    plt.close(fig)


def run_validation(
    tag: str,
    *,
    iterations: int = 1000,
    block_size: int = 3,
    capital_scenarios: int = 3,
    check_completeness: bool = False,
) -> dict[str, object]:
    metrics_path = ROOT / "output" / "ablation" / f"metrics_{tag}.json"
    nav_path = ROOT / "output" / "ablation" / f"nav_series_{tag}.csv"
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    execution = payload["full_execution"]
    full_metrics = payload["metrics"]["full"]
    yearly = pd.DataFrame(payload["yearly_results"])
    if yearly.empty:
        raise ValueError("主回测缺少逐年结果；请用新版 --fusion 命令重新运行")
    nav_frame = pd.read_csv(nav_path, index_col=0, parse_dates=True)
    nav = nav_frame["full"].astype(float)

    candidate_counts = pd.Series(execution["candidate_stage_counts"]["final"], dtype=float)
    depletion_frequency = float(candidate_counts.le(9).mean())
    annual_std = float(yearly["annual_return"].std(ddof=1))
    max_contribution = float(yearly["cumulative_return_contribution"].max())
    annual_pass = bool(len(yearly) == 10 and annual_std < 0.10)
    candidate_pass = depletion_frequency < 0.20

    turnover_path = ROOT / "output" / "turnover" / "turnover_analysis.json"
    turnover_data = json.loads(turnover_path.read_text(encoding="utf-8"))
    buffer_dependency = float(turnover_data.get("buffer_dependency", 0.0))
    buffer_pass = buffer_dependency <= 0.30

    selections = execution["selected_symbols"]
    symbols = sorted({symbol for selected in selections.values() for symbol in selected})
    returns = load_returns(symbols, str(nav.index.min().date()), str(nav.index.max().date()))
    random_drop, shifted = stress_tests(returns, selections, n_iter=iterations)
    random_pass = random_drop["p05"] > 0.10
    shifted_pass = shifted["p05"] > 0.10

    daily = nav.pct_change()
    rolling_values = []
    years = sorted(set(nav.index.year))
    for start in years:
        end = start + 2
        if end not in years:
            continue
        sample = daily.loc[(daily.index.year >= start) & (daily.index.year <= end)].dropna()
        rolling_values.append(float((1.0 + sample).prod() ** (252.0 / len(sample)) - 1.0))
    rolling = {
        "values": rolling_values,
        "mean": float(np.mean(rolling_values)),
        "std": float(np.std(rolling_values, ddof=1)),
        "min": float(np.min(rolling_values)),
    }
    rolling_pass = rolling["mean"] > 0.10 and rolling["min"] > 0.0
    fusion_pass = bool(
        candidate_counts.min() >= 50
        and float(full_metrics["annual_return"]) >= 0.16
        and float(full_metrics["sharpe_ratio"]) >= 0.80
        and float(full_metrics["turnover"]) <= 0.25
    )

    bootstrap = run_block_bootstrap(
        tag,
        iterations=iterations,
        block_size=block_size,
        threshold=0.05,
        output=BLOCK_OUTPUT,
    )
    capital = run_incremental_capital_test(
        tag,
        scenarios=capital_scenarios,
        capital_levels=(100000.0, 200000.0, 500000.0),
        output=CAPITAL_OUTPUT,
    )
    checks = [
        ("annual_stability", annual_pass),
        ("candidate_pool_health", candidate_pass),
        ("buffer_dependency", buffer_pass),
        ("random_drop_20pct", random_pass),
        ("rebalance_shift_13d", shifted_pass),
        ("rolling_3y", rolling_pass),
        ("fusion_ranking", fusion_pass),
        ("block_bootstrap", bool(bootstrap["passed"])),
        ("incremental_capital", bool(capital["passed"])),
    ]
    passed = sum(int(value) for _, value in checks)
    decision = (
        "production_ready" if passed >= 6
        else "conditional_approval" if passed >= 4
        else "failed"
    )
    control_path = ROOT / "output" / "ablation" / "metrics_fusion_v2_control.json"
    control_payload = json.loads(control_path.read_text(encoding="utf-8"))
    control_metrics = control_payload["metrics"]["full"]
    result: dict[str, object] = {
        "tag": tag,
        "passed": passed,
        "total": 9,
        "acceptance_threshold": 6,
        "accepted": passed >= 6,
        "decision": decision,
        "checks": {name: value for name, value in checks},
        "annual_stability": {
            "year_count": int(len(yearly)),
            "annual_return_std": annual_std,
            "max_single_year_contribution": max_contribution,
        },
        "candidate_pool": {
            "min": float(candidate_counts.min()),
            "mean": float(candidate_counts.mean()),
            "depletion_frequency": depletion_frequency,
        },
        "buffer_dependency": {
            "value": buffer_dependency,
            "source": str(turnover_path.relative_to(ROOT)),
            "reuse_reason": "fusion_v2 不使用旧式 max-sell buffer；因子等权不改变该机制定义。",
        },
        "random_drop": random_drop,
        "shifted_rebalance": shifted,
        "rolling_3y": rolling,
        "fusion": full_metrics,
        "block_bootstrap": bootstrap,
        "incremental_capital": {
            key: value for key, value in capital.items() if key != "scenarios"
        },
        "iterations": int(iterations),
        "factor_pool": execution.get("fusion_factors", []),
        "factor_weights": execution.get("fusion_weights", {}),
        "control_comparison": {
            "source": str(control_path.relative_to(ROOT)),
            "factors": control_payload["full_execution"].get("fusion_factors", []),
            "metrics": control_metrics,
        },
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    results_path = OUTPUT / "comprehensive_results_fixed.json"
    report_path = OUTPUT / "comprehensive_report_fixed.md"
    results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    status = lambda value: "PASS" if value else "FAIL"
    decision_label = {
        "production_ready": "生产就绪",
        "conditional_approval": "有条件批准（未达生产门槛）",
        "failed": "未通过",
    }[decision]
    lines = [
        "# fusion_v2 审计整改综合验收报告",
        "",
        f"结论：通过 **{passed}/9** 项（门槛 6/9），最终状态：**{decision_label}**。",
        "",
        f"主配置因子池：{', '.join(f'`{value}`' for value in result['factor_pool'])}；池内严格等权。",
        "",
        "| 序号 | 验收项 | 实测 | 门槛 | 状态 |",
        "|---:|---|---:|---:|:---:|",
        f"| 1 | 年度稳定性 | 10年；收益标准差 {annual_std:.2%} | 标准差<10% | {status(annual_pass)} |",
        f"| 2 | 候选池健康度 | 最少 {candidate_counts.min():.0f}；≤9 枯竭频率 {depletion_frequency:.2%} | <20% | {status(candidate_pass)} |",
        f"| 3 | 缓冲依赖 | {buffer_dependency:.2%} | ≤30% | {status(buffer_pass)} |",
        f"| 4 | 随机剔除20% | 5%分位 {random_drop['p05']:.2%} | >10% | {status(random_pass)} |",
        f"| 5 | 调仓偏移±13日 | 5%分位 {shifted['p05']:.2%} | >10% | {status(shifted_pass)} |",
        f"| 6 | 滚动3年 | 均值 {rolling['mean']:.2%}；最小 {rolling['min']:.2%} | 均值>10%，全部>0% | {status(rolling_pass)} |",
        f"| 7 | 融合排序 | 候选≥{candidate_counts.min():.0f}；年化 {full_metrics['annual_return']:.2%}；夏普 {full_metrics['sharpe_ratio']:.2f}；换手 {full_metrics['turnover']:.2%} | ≥50、≥16%、≥0.80、≤25% | {status(fusion_pass)} |",
        f"| 8 | Block Bootstrap | P5 {bootstrap['summary']['p05']:.2%} | ≥5% | {status(bool(bootstrap['passed']))} |",
        f"| 9 | 增量资金 | {sum(item['passed'] for item in capital['scenarios'])}/{capital_scenarios} 场景通过 | 全部通过 | {status(bool(capital['passed']))} |",
        "",
        "## 复用口径",
        "",
        "候选池健康度从本次主回测重新读取；缓冲依赖复用机制审计结果，因为 fusion_v2 未启用旧式卖出数量缓冲。其余收益相关项目均基于整改后主配置重新计算。",
        "",
        "## 主配置与固定对照",
        "",
        "| 配置 | 因子池 | 年化收益 | 夏普 | 最大回撤 | 换手率 |",
        "|---|---|---:|---:|---:|---:|",
        f"| 主配置 | {', '.join(execution.get('fusion_factors', []))} | {full_metrics['annual_return']:.2%} | {full_metrics['sharpe_ratio']:.2f} | {full_metrics['max_drawdown']:.2%} | {full_metrics['turnover']:.2%} |",
        f"| 固定对照 | {', '.join(control_payload['full_execution'].get('fusion_factors', []))} | {control_metrics['annual_return']:.2%} | {control_metrics['sharpe_ratio']:.2f} | {control_metrics['max_drawdown']:.2%} | {control_metrics['turnover']:.2%} |",
        "",
        "固定对照只用于展示因子裁决的机会成本，不替代 t 统计闸门裁决出的主配置。",
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    _plot_main_vs_control(nav, ROOT / "output" / "ablation" / "nav_comparison_fixed.png")

    if check_completeness:
        required = [
            ROOT / "output" / "t_stats" / "factor_t_stats.csv",
            ROOT / "output" / "orthogonality" / "orthogonality_report_fixed.md",
            ROOT / "output" / "ablation" / "metrics_fusion_v2_fixed.json",
            ROOT / "output" / "ablation" / "metrics_fusion_v2_control.json",
            ROOT / "output" / "ablation" / "nav_comparison_fixed.png",
            results_path,
            report_path,
            BLOCK_OUTPUT,
            CAPITAL_OUTPUT,
        ]
        missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file() or path.stat().st_size == 0]
        if missing:
            raise FileNotFoundError(f"核心交付物缺失或为空：{', '.join(missing)}")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Nine-item fusion_v2 validation")
    parser.add_argument("--tag", default="fusion_v2_fixed")
    parser.add_argument("--items", type=int, default=9)
    parser.add_argument("--bootstrap-iterations", "--iterations", type=int, default=1000)
    parser.add_argument("--block-size", type=int, default=3)
    parser.add_argument("--capital-scenarios", type=int, default=3)
    parser.add_argument("--full", action="store_true", help="兼容命令；固定运行 1000 次")
    parser.add_argument("--check-completeness", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.items != 9:
        raise ValueError("锁定协议要求 items 必须等于 9，禁止删减")
    iterations = 1000 if args.full else args.bootstrap_iterations
    if str(args.tag).startswith("baseline_h30269"):
        from scripts.baseline_validation import run_baseline_validation

        result = run_baseline_validation(args.tag, iterations=iterations)
        print(json.dumps({"passed": result["passed"], "accepted": result["accepted"], "checks": result["checks"]}, ensure_ascii=False, indent=2))
        return 0 if result["accepted"] else 1
    result = run_validation(
        args.tag,
        iterations=iterations,
        block_size=args.block_size,
        capital_scenarios=args.capital_scenarios,
        check_completeness=args.check_completeness,
    )
    print(json.dumps({"passed": result["passed"], "accepted": result["accepted"], "decision": result["decision"], "checks": result["checks"]}, ensure_ascii=False, indent=2))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
