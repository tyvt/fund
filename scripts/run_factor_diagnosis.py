#!/usr/bin/env python
"""Command-line entry point for factor diagnosis."""

from __future__ import annotations

import argparse
from copy import deepcopy
import logging
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from alphapurify_bridge.config import load_diagnosis_config, load_factor_registry
from alphapurify_bridge.diagnostics import DiagnosisRunner
from alphapurify_bridge.io import persist_results, update_registry_statuses, write_approved_factors
from alphapurify_bridge.reporting import DiagnosisReporter
from alphapurify_bridge.utils import PerformanceLog, merge_stages


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AlphaPurify 因子诊断流水线")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--factor", help="诊断单个因子")
    selection.add_argument("--factors", help="逗号分隔的因子列表")
    selection.add_argument("--all", action="store_true", help="诊断注册表中的全部因子")
    parser.add_argument("--start", help="覆盖起始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="覆盖结束日期 YYYY-MM-DD")
    parser.add_argument("--horizon", type=int, help="覆盖本次诊断的主预测期（交易日）")
    parser.add_argument("--config", default="config/alphapurify/diagnosis_config.yaml")
    parser.add_argument("--registry", default="config/alphapurify/factor_registry.yaml")
    parser.add_argument("--report", action="store_true", help="生成单因子和批量报告")
    parser.add_argument("--format", default="md,html", help="报告格式：md、html 或二者")
    parser.add_argument("--official", action="store_true", help="同时运行官方 AlphaPurify 1.0.6 审计（较慢）")
    parser.add_argument("--fast", action="store_true", help="显式快速模式：5 层、最多 3 个预测期")
    parser.add_argument("--profile", action="store_true", help="记录分阶段性能日志")
    parser.add_argument("--perf-log", default="logs/alphapurify_perf.log", help="性能 JSONL 日志路径")
    return parser.parse_args()


def _fast_config(config: dict[str, object]) -> dict[str, object]:
    selected = deepcopy(config)
    diagnosis = selected["diagnosis"]
    horizons = list(diagnosis["horizons"])
    primary = int(diagnosis.get("primary_horizon", horizons[0]))
    preferred = [value for value in (1, 5, 20) if value in horizons]
    if primary not in preferred:
        preferred.insert(0, primary)
    diagnosis["horizons"] = list(dict.fromkeys(preferred or horizons[:3]))[:3]
    diagnosis["n_quantiles"] = 5
    return selected


def main(args: argparse.Namespace) -> int:
    config = load_diagnosis_config(args.config)
    if args.fast:
        config = _fast_config(config)
    registry = load_factor_registry(args.registry)
    if args.horizon is not None:
        if args.horizon < 1:
            raise ValueError("--horizon 必须为正整数")
        horizons = list(config["diagnosis"]["horizons"])
        if args.horizon not in horizons:
            horizons.append(args.horizon)
        config["diagnosis"]["horizons"] = horizons
        config["diagnosis"]["primary_horizon"] = args.horizon
        for metadata in registry["factors"].values():
            metadata["primary_horizon"] = args.horizon
    runner = DiagnosisRunner(config, registry=registry)
    if args.factor:
        factors = [args.factor]
    elif args.factors:
        factors = [value.strip() for value in args.factors.split(",") if value.strip()]
    else:
        factors = runner.factor_names
    started = time.perf_counter()
    results = runner.diagnose_factors(
        factors,
        args.start,
        args.end,
        official=args.official,
        profile=args.profile,
    )
    output_root = config["output"].get("output_dir", "output/alphapurify")
    serialize_started = time.perf_counter()
    artifacts = persist_results(results, output_root)
    approved = write_approved_factors(
        results,
        config["output"].get("approved_factors_file", "output/alphapurify/approved_factors.json"),
        diagnosis_artifact=artifacts["json"],
    )
    update_registry_statuses(results, args.registry)
    serialize_elapsed = time.perf_counter() - serialize_started
    report_paths: list[Path] = []
    factor_report_seconds = {name: 0.0 for name in factors}
    report_started = time.perf_counter()
    if args.report:
        reporter = DiagnosisReporter(Path(output_root) / "reports")
        formats = list(dict.fromkeys(value.strip().lower() for value in args.format.split(",") if value.strip()))
        for result in results:
            factor_started = time.perf_counter()
            report_paths.extend(reporter.generate_factor_reports(result, formats))
            factor_report_seconds[str(result["factor_name"])] = time.perf_counter() - factor_started
        for output_format in formats:
            result_horizons = {int(result["primary_horizon"]) for result in results}
            stem = (
                f"batch_diagnosis_{next(iter(result_horizons))}d"
                if len(result_horizons) == 1
                else "batch_diagnosis_mixed"
            )
            report_paths.append(
                reporter.generate_batch_report(results, output_format, stem=stem)
            )
    report_elapsed = time.perf_counter() - report_started if args.report else 0.0
    total_elapsed = time.perf_counter() - started
    if args.profile:
        perf_log = PerformanceLog(args.perf_log)
        serialize_share = serialize_elapsed / max(1, len(results))
        for result in results:
            name = str(result["factor_name"])
            item = runner.last_profiles[name]
            stages = merge_stages(
                item["stages"],
                {"serialize": serialize_share, "report": factor_report_seconds[name]},
            )
            perf_log.factor(
                name,
                stages=stages,
                total=float(item["total"]) + serialize_share + factor_report_seconds[name],
                rows=int(result.get("sample_count", 0)),
                symbols=item.get("symbols"),
                dates=int(result.get("cross_section_count", 0)),
                mode="fast" if args.fast else "full",
            )
        batch_profile = runner.last_batch_profile
        perf_log.batch(
            factors,
            stages=merge_stages(
                batch_profile.get("stages"),
                {"serialize": serialize_elapsed, "report": report_elapsed},
            ),
            total=total_elapsed,
            per_factor=batch_profile.get("per_factor", {}),
            mode="fast" if args.fast else "full",
        )
        print(f"性能日志：{perf_log.path}")
    print(f"因子诊断完成：{len(results)} 个，耗时 {total_elapsed:.2f} 秒")
    for result in results:
        print(
            f"{result['factor_name']}: {result['status']} | IC={result['ic_mean']:.4f} | "
            f"IR={result['ic_ir']:.4f} | Spread={result['spread_return']:.2%}"
        )
    print(f"结果：{artifacts['json']}")
    print(f"VectorBT 批准清单：{approved}")
    for path in report_paths:
        print(f"报告：{path}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main(parse_args()))
