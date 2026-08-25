#!/usr/bin/env python
"""Run the VectorBT backtest/report or a configuration-driven parameter scan."""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vbt.adapters import DEFAULT_FACTORS, VBTDataLoader
from vbt.config import load_backtest_config, load_scan_config, load_strategy_config
from vbt.engine import PerformanceCalculator, ReportGenerator, VBTEngine
from vbt.engine.parameter_scan import ParameterScan
from vbt.strategies import DividendLowVolStrategy


def _engine(data, strategy, config):
    return VBTEngine(
        data=data,
        strategy=strategy,
        initial_capital=float(config["initial_capital"]),
        commission=float(config["commission"]),
        min_commission=float(config["min_commission"]),
        stamp_duty_before=float(config["stamp_duty_before_2023_08_28"]),
        stamp_duty_after=float(config["stamp_duty_after_2023_08_28"]),
        slippage=float(config["slippage"]),
        backtest_config=config,
    )


def run_quick(args) -> int:
    overrides = {}
    if args.start:
        overrides["start_date"] = args.start
    if args.end:
        overrides["end_date"] = args.end
    if args.capital is not None:
        overrides["initial_capital"] = args.capital
    config = load_backtest_config(overrides)
    params = load_strategy_config()
    started = time.perf_counter()
    loader = VBTDataLoader(
        start_date=config["start_date"],
        end_date=config["end_date"],
        cache_enabled=bool(config.get("cache_enabled", True)),
        cache_dir=config.get("cache_dir", "cache/vectorbt"),
    )
    use_frozen = not args.compile_rules and not args.start and not args.end
    data = (
        loader.load_baseline_aligned(
            config["baseline_path"], initial_capital=float(config["initial_capital"])
        )
        if use_frozen
        else loader.load_aligned(verbose=args.verbose)
    )
    results = _engine(data, DividendLowVolStrategy(params), config).run(verbose=args.verbose)
    perf = PerformanceCalculator(results)
    paths = ReportGenerator(results, perf, params).archive(config["output_dir"])
    elapsed = time.perf_counter() - started
    metrics = perf.compute_metrics()
    print(f"VectorBT 回测完成：{elapsed:.2f} 秒")
    print(
        f"累计收益 {metrics['total_return']:.2%} | 年化收益 {metrics['annual_return']:.2%} | "
        f"最大回撤 {metrics['max_drawdown']:.2%} | 夏普 {metrics['sharpe_ratio']:.4f}"
    )
    print(f"HTML 报告：{paths['html']}")
    print(f"Markdown：{paths['markdown']}")
    print(f"结果数据：{paths['run_dir']}")
    if args.open:
        webbrowser.open(paths["html"].resolve().as_uri())
    return 0


def run_scan(args) -> int:
    config_overrides = {}
    if args.start:
        config_overrides["start_date"] = args.start
    if args.end:
        config_overrides["end_date"] = args.end
    if args.capital is not None:
        config_overrides["initial_capital"] = args.capital
    config = load_backtest_config(config_overrides)
    scan = load_scan_config(args.param_file)
    params = load_strategy_config({"alignment_mode": False})
    loader = VBTDataLoader(
        start_date=config["start_date"],
        end_date=config["end_date"],
        cache_enabled=bool(config.get("cache_enabled", True)),
        cache_dir=config.get("cache_dir", "cache/vectorbt"),
    )
    started = time.perf_counter()
    data = loader.load(factors=DEFAULT_FACTORS, include_prices=True)
    engine = _engine(data, DividendLowVolStrategy(params), config)
    results = ParameterScan(
        engine=engine,
        param_grid=scan["param_grid"],
        metric=scan.get("metric", "sharpe_ratio"),
    ).run(n_jobs=int(scan.get("n_jobs", -1)))
    output = Path(scan.get("output_dir", "output/vectorbt/param_scans"))
    if not output.is_absolute():
        output = ROOT / output
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = results.to_parquet(output / f"scan_{stamp}.parquet")
    (output / f"scan_{stamp}.json").write_text(
        json.dumps({"best_params": results.best_params(), "metric": results.metric}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"参数扫描完成：{len(results.table)} 组，{time.perf_counter() - started:.2f} 秒")
    print(f"最佳参数：{results.best_params()}")
    print(f"扫描结果：{path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VectorBT 红利低波研究流水线")
    parser.add_argument("--mode", choices=("quick", "scan"), default="quick")
    parser.add_argument("--start", help="覆盖起始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="覆盖结束日期 YYYY-MM-DD")
    parser.add_argument("--capital", type=float, help="覆盖初始资金")
    parser.add_argument("--param-file", default="config/vectorbt/scan_params.yaml")
    parser.add_argument("--open", action="store_true", help="回测完成后用浏览器打开 HTML")
    parser.add_argument("--compile-rules", action="store_true", help="忽略冻结基准，重新编译完整生产规则（冷启动较慢）")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    raise SystemExit(run_quick(arguments) if arguments.mode == "quick" else run_scan(arguments))
