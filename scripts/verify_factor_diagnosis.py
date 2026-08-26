#!/usr/bin/env python
"""Acceptance and repeatable performance checks for AlphaPurify diagnosis."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import psutil


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from alphapurify_bridge.adapters import SnapshotAdapter
from alphapurify_bridge.config import load_diagnosis_config
from alphapurify_bridge.diagnostics import DiagnosisRunner, compute_ic
from alphapurify_bridge.filters import ThresholdFilter
from alphapurify_bridge.io import json_safe
from alphapurify_bridge.reporting import DiagnosisReporter
from alphapurify_bridge.utils import PERF_STAGE_NAMES, PerformanceLog, merge_stages


NUMERIC_RESULT_KEYS = (
    "sample_count",
    "cross_section_count",
    "ic_mean",
    "ic_ir",
    "ic_by_horizon",
    "ic_decay",
    "quantile_returns",
    "spread_return",
    "monotonicity_rank_corr",
    "factor_return_mean",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验收 AlphaPurify 因子诊断系统")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true", help="六因子十年完整性能验收（保持 10 层）")
    mode.add_argument("--fast", action="store_true", help="显式快速模式：5 层、最多 3 个预测期")
    parser.add_argument("--official", action="store_true", help="增加官方 AlphaPurify 抽检")
    parser.add_argument("--profile", action="store_true", help="写入分阶段 JSONL 性能日志")
    parser.add_argument("--perf-log", default="logs/alphapurify_perf.log", help="性能日志路径")
    parser.add_argument("--runs", type=int, default=3, help="性能场景连续运行次数")
    parser.add_argument("--save-baseline", type=Path, help="保存本次结构化性能基准")
    return parser.parse_args()


class _MemorySampler:
    def __init__(self, process: psutil.Process, interval: float = 0.05):
        self.process = process
        self.interval = interval
        self.start_rss = process.memory_info().rss
        self.peak_rss = self.start_rss
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.peak_rss = max(self.peak_rss, self.process.memory_info().rss)
            except psutil.Error:
                return

    def __enter__(self) -> "_MemorySampler":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.peak_rss = max(self.peak_rss, self.process.memory_info().rss)
        self._stop.set()
        self._thread.join(timeout=1.0)

    @property
    def delta(self) -> int:
        return max(0, self.peak_rss - self.start_rss)


def _mark(name: str, passed: bool, detail: str) -> dict[str, object]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _configure(args: argparse.Namespace) -> dict[str, Any]:
    config = deepcopy(load_diagnosis_config())
    diagnosis = config["diagnosis"]
    if not args.full:
        diagnosis.update({"start_date": "2023-01-01", "end_date": "2024-12-31"})
    if args.fast:
        horizons = list(diagnosis["horizons"])
        primary = int(diagnosis.get("primary_horizon", horizons[0]))
        selected = [value for value in (1, 5, 20) if value in horizons]
        if primary not in selected:
            selected.insert(0, primary)
        diagnosis["horizons"] = list(dict.fromkeys(selected or horizons[:3]))[:3]
        diagnosis["n_quantiles"] = 5
    return config


def _numeric_projection(result: Mapping[str, Any]) -> dict[str, Any]:
    return {key: json_safe(result.get(key)) for key in NUMERIC_RESULT_KEYS}


def _numeric_values(value: Any) -> list[float]:
    if isinstance(value, Mapping):
        output: list[float] = []
        for item in value.values():
            output.extend(_numeric_values(item))
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        output = []
        for item in value:
            output.extend(_numeric_values(item))
        return output
    if value is None or isinstance(value, bool):
        return []
    try:
        number = float(value)
    except (TypeError, ValueError):
        return []
    return [number] if np.isfinite(number) else []


def _max_numeric_diff(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    left_values = _numeric_values(left)
    right_values = _numeric_values(right)
    if len(left_values) != len(right_values):
        return float("inf")
    return max((abs(a - b) for a, b in zip(left_values, right_values)), default=0.0)


def _average_stages(profiles: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    count = max(1, len(profiles))
    return {
        stage: sum(float(item.get("stages", {}).get(stage, 0.0)) for item in profiles) / count
        for stage in PERF_STAGE_NAMES
    }


def _write_validation_report(checks: Sequence[Mapping[str, object]]) -> Path:
    output = ROOT / "output" / "alphapurify" / "validation_report.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(
        f"| {'✅' if item['passed'] else '❌'} | {item['check']} | {item['detail']} |"
        for item in checks
    )
    output.write_text(
        "# AlphaPurify 接入验收报告\n\n| 状态 | 检查 | 结果 |\n|---|---|---|\n" + rows + "\n",
        encoding="utf-8",
    )
    return output


def main(args: argparse.Namespace) -> int:
    if args.runs < 1:
        raise ValueError("--runs 必须大于 0")
    config = _configure(args)
    diagnosis = config["diagnosis"]
    benchmark_start = "2015-01-01" if args.full else diagnosis["start_date"]
    benchmark_end = "2024-12-31" if args.full else diagnosis.get("end_date")
    mode = "fast" if args.fast else "full"
    adapter = SnapshotAdapter()
    runner = DiagnosisRunner(config, adapter=adapter)
    checks: list[dict[str, object]] = []
    process = psutil.Process()

    with _MemorySampler(process) as memory:
        loaded = adapter.load_factor("dividend_yield", benchmark_start, benchmark_end, horizon=1)
        checks.append(
            _mark(
                "SnapshotAdapter 加载因子与未来收益",
                list(loaded.columns) == ["trade_date", "symbol", "factor_value", "forward_return"]
                and not loaded.empty,
                f"rows={len(loaded):,}",
            )
        )
        checks.append(
            _mark(
                "日期范围提前裁剪",
                loaded["trade_date"].min() >= pd.Timestamp(benchmark_start)
                and loaded["trade_date"].max() <= pd.Timestamp(benchmark_end),
                f"{loaded['trade_date'].min().date()} ~ {loaded['trade_date'].max().date()}",
            )
        )

        single_times: list[float] = []
        single_profiles: list[dict[str, Any]] = []
        single_results: list[dict[str, Any]] = []
        for _run in range(args.runs):
            started = time.perf_counter()
            result = runner.diagnose_factor(
                "dividend_yield",
                benchmark_start,
                benchmark_end,
                official=False,
                profile=True,
            )
            single_times.append(time.perf_counter() - started)
            single_results.append(result)
            single_profiles.append(deepcopy(runner.last_profiles["dividend_yield"]))

        factors = runner.factor_names if args.full else ["dividend_yield"]
        batch_times: list[float] = []
        batch_profiles: list[dict[str, Any]] = []
        batch_results: list[list[dict[str, Any]]] = []
        for _run in range(args.runs):
            started = time.perf_counter()
            results = runner.diagnose_factors(
                factors,
                benchmark_start,
                benchmark_end,
                official=args.official,
                profile=True,
            )
            batch_times.append(time.perf_counter() - started)
            batch_results.append(results)
            batch_profiles.append(deepcopy(runner.last_batch_profile))

        sample = loaded.dropna().sample(min(200_000, len(loaded)), random_state=20260826).copy()
        sample["factor_value"] = np.random.default_rng(20260826).normal(size=len(sample))
        noise_ic = compute_ic(sample, min_observations=10)
        filter_result = ThresholdFilter(config["thresholds"]).evaluate(
            {
                "ic_mean": 0.02,
                "ic_ir": 0.5,
                "spread_return": 0.05,
                "quantile_monotonicity": True,
                "ic_decay": {"horizon_1": 0.0, "horizon_5": 0.8},
            }
        )

        report_started = time.perf_counter()
        reporter = DiagnosisReporter("output/alphapurify/reports")
        md, html = reporter.generate_factor_reports(batch_results[-1][0], ("md", "html"))
        batch_md = reporter.generate_batch_report(batch_results[-1], "md")
        batch_html = reporter.generate_batch_report(batch_results[-1], "html")
        report_elapsed = time.perf_counter() - report_started

    single_average = sum(single_times) / len(single_times)
    batch_average = sum(batch_times) / len(batch_times)
    single_diff = max(
        _max_numeric_diff(_numeric_projection(single_results[0]), _numeric_projection(item))
        for item in single_results[1:]
    ) if len(single_results) > 1 else 0.0
    batch_reference = {item["factor_name"]: _numeric_projection(item) for item in batch_results[0]}
    batch_diff = max(
        (
            _max_numeric_diff(batch_reference[item["factor_name"]], _numeric_projection(item))
            for run_results in batch_results[1:]
            for item in run_results
        ),
        default=0.0,
    )

    checks.extend(
        [
            _mark("单因子连续运行", len(single_results) == args.runs, f"runs={args.runs}"),
            _mark("单因子十年诊断不超过 5 秒", single_average <= 5.0, f"average={single_average:.2f}s; runs={single_times}"),
            _mark("DiagnosisRunner 多因子诊断", len(batch_results[-1]) == len(factors), f"factors={len(factors)}"),
            _mark("六因子批量诊断不超过 30 秒", batch_average <= 30.0, f"average={batch_average:.2f}s; runs={batch_times}"),
            _mark("缓存前后单因子数值一致", single_diff < 1e-6, f"max_diff={single_diff:.3g}"),
            _mark("缓存前后批量数值一致", batch_diff < 1e-6, f"max_diff={batch_diff:.3g}"),
            _mark("随机噪声 IC 接近零", abs(float(noise_ic.mean())) < 0.01, f"IC={noise_ic.mean():.5f}"),
            _mark(
                "衰减警告不覆盖最终 PASS",
                filter_result["status"] == "PASS"
                and filter_result["checks"]["ic_decay"]["status"] == "WARNING",
                filter_result["summary"],
            ),
            _mark("Markdown + HTML 报告", all(path.is_file() for path in (md, html, batch_md, batch_html)), f"elapsed={report_elapsed:.2f}s"),
            _mark("内存增量低于 4GB", memory.delta < 4 * 1024**3, f"peak_delta={memory.delta / 1024**3:.2f}GB"),
        ]
    )

    if args.profile:
        perf_log = PerformanceLog(args.perf_log)
        for index, (duration, profile) in enumerate(zip(single_times, single_profiles), 1):
            perf_log.factor(
                "dividend_yield",
                stages=profile["stages"],
                total=duration,
                rows=int(profile["rows"]),
                symbols=profile.get("symbols"),
                dates=int(profile["dates"]),
                run=index,
                mode=mode,
            )
        for index, (duration, profile) in enumerate(zip(batch_times, batch_profiles), 1):
            perf_log.batch(
                factors,
                stages=profile["stages"],
                total=duration,
                per_factor=profile["per_factor"],
                run=index,
                mode=mode,
            )
        perf_log.append(
            {
                "timestamp": PerformanceLog._timestamp(),
                "type": "report",
                "mode": mode,
                "stages": merge_stages({"report": report_elapsed}),
                "total": report_elapsed,
                "factors": factors,
            }
        )

    baseline = {
        "schema_version": 1,
        "timestamp": PerformanceLog._timestamp(),
        "mode": mode,
        "date_range": {"start": benchmark_start, "end": benchmark_end},
        "runs": args.runs,
        "single_factor": {
            "factor": "dividend_yield",
            "durations": single_times,
            "average": single_average,
            "stages": _average_stages(single_profiles),
        },
        "batch": {
            "factors": factors,
            "durations": batch_times,
            "average": batch_average,
            "stages": _average_stages(batch_profiles),
        },
        "report_seconds": report_elapsed,
        "memory_delta_gb": memory.delta / 1024**3,
        "max_numeric_diff": max(single_diff, batch_diff),
    }
    if args.save_baseline:
        target = args.save_baseline if args.save_baseline.is_absolute() else ROOT / args.save_baseline
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(json_safe(baseline), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"性能基准：{target}")

    output = _write_validation_report(checks)
    failed = [item for item in checks if not item["passed"]]
    print(f"验收完成：{len(checks) - len(failed)}/{len(checks)} 通过")
    print(f"报告：{output}")
    if args.profile:
        print(f"性能日志：{PerformanceLog(args.perf_log).path}")
    for item in failed:
        print(f"FAIL {item['check']}: {item['detail']}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(parse_args()))
