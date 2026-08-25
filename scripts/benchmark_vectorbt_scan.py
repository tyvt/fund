#!/usr/bin/env python
"""Run the formal 1,000-combination VectorBT scan acceptance benchmark."""

from __future__ import annotations

import argparse
import json
import platform
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import psutil


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_vectorbt_pipeline import _engine
from vbt.adapters import DEFAULT_FACTORS, VBTDataLoader
from vbt.config import load_backtest_config, load_strategy_config
from vbt.engine.parameter_scan import ParameterScan
from vbt.strategies import DividendLowVolStrategy


def _memory_monitor(stop: threading.Event, samples: list[int]) -> None:
    process = psutil.Process()
    while not stop.wait(0.20):
        try:
            samples.append(process.memory_info().rss)
        except psutil.Error:
            return


def main() -> int:
    parser = argparse.ArgumentParser(description="VectorBT 1000 组参数扫描性能验收")
    parser.add_argument("--output-dir", default="output/vectorbt/param_scans")
    parser.add_argument("--n-jobs", type=int, default=-1)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    config = load_backtest_config()
    params = load_strategy_config({"alignment_mode": False})
    grid = {
        "top_n": list(range(5, 25)),
        "volatility_60d_max": [round(float(value), 4) for value in np.linspace(0.15, 0.39, 25)],
        "rebalance_freq": ["A", "Q"],
    }
    group_count = int(np.prod([len(values) for values in grid.values()]))
    if group_count != 1000:
        raise AssertionError(f"验收网格应为 1000 组，实际 {group_count} 组")

    samples = [psutil.Process().memory_info().rss]
    stop = threading.Event()
    monitor = threading.Thread(target=_memory_monitor, args=(stop, samples), daemon=True)
    monitor.start()
    total_started = time.perf_counter()
    load_started = total_started
    loader = VBTDataLoader(
        start_date=config["start_date"],
        end_date=config["end_date"],
        cache_enabled=bool(config.get("cache_enabled", True)),
        cache_dir=config.get("cache_dir", "cache/vectorbt"),
    )
    data = loader.load(factors=DEFAULT_FACTORS, include_prices=True)
    load_seconds = time.perf_counter() - load_started
    scan_started = time.perf_counter()
    results = ParameterScan(
        engine=_engine(data, DividendLowVolStrategy(params), config),
        param_grid=grid,
        metric="sharpe_ratio",
    ).run(n_jobs=args.n_jobs)
    scan_seconds = time.perf_counter() - scan_started
    total_seconds = time.perf_counter() - total_started
    stop.set()
    monitor.join(timeout=2.0)
    samples.append(psutil.Process().memory_info().rss)

    errors = int(results.table["status"].ne("ok").sum())
    peak_gib = max(samples) / 1024**3
    passed = len(results.table) == 1000 and errors == 0 and total_seconds < 300 and peak_gib < 8
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_path = results.to_parquet(output_dir / f"benchmark_1000_{stamp}.parquet")
    report_path = ROOT / "output/vectorbt/reports/scan_benchmark.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "date_range": [str(config["start_date"]), str(config["end_date"])],
        "groups": len(results.table),
        "errors": errors,
        "data_load_seconds": load_seconds,
        "scan_seconds": scan_seconds,
        "total_seconds": total_seconds,
        "peak_memory_gib": peak_gib,
        "limits": {"total_seconds": 300, "peak_memory_gib": 8},
        "passed": passed,
        "best_params": results.best_params(),
        "result_path": str(result_path.relative_to(ROOT)),
    }
    (output_dir / f"benchmark_1000_{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    report_path.write_text(
        "\n".join(
            [
                "# VectorBT 1000 组参数扫描性能验收",
                "",
                f"- 验收结论：**{'通过' if passed else '未通过'}**",
                f"- 参数组合：{len(results.table):,} 组（失败 {errors} 组）",
                f"- 数据加载：{load_seconds:.2f} 秒",
                f"- 参数扫描：{scan_seconds:.2f} 秒",
                f"- 端到端耗时：{total_seconds:.2f} 秒（要求 < 300 秒）",
                f"- 峰值进程内存：{peak_gib:.2f} GiB（要求 < 8 GiB）",
                f"- 最佳参数：`{json.dumps(results.best_params(), ensure_ascii=False, default=str)}`",
                f"- 完整结果：`{result_path.relative_to(ROOT)}`",
                "",
                "> 扫描使用稀疏调仓区间模拟；入选参数需由正式 VectorBT/RQAlpha 对齐流程复核。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    print(f"报告：{report_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
