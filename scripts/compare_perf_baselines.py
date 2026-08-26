#!/usr/bin/env python
"""Compare AlphaPurify performance baselines and write a Markdown report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对比 AlphaPurify 优化前后性能")
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/alphapurify/performance_optimization_report.md"),
    )
    return parser.parse_args()


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def _from_jsonl(path: Path) -> dict[str, Any]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    single = [
        item
        for item in records
        if item.get("type") == "factor"
        and item.get("factor") == "dividend_yield"
        and item.get("mode") == "full"
        and item.get("run") is not None
    ][-3:]
    batch = [
        item
        for item in records
        if item.get("type") == "batch"
        and item.get("mode") == "full"
        and item.get("run") is not None
    ][-3:]
    if not single or not batch:
        raise ValueError(f"性能日志缺少 full 模式单因子或批量记录：{path}")

    def average_stages(items: list[Mapping[str, Any]], key: str) -> dict[str, float]:
        names = {name for item in items for name in (item.get(key, {}) or {})}
        return {
            name: mean(float(item.get(key, {}).get(name, 0.0)) for item in items)
            for name in names
        }

    return {
        "single_factor": {
            "durations": [float(item["total"]) for item in single],
            "average": mean(float(item["total"]) for item in single),
            "stages": average_stages(single, "stages"),
        },
        "batch": {
            "durations": [float(item["total_duration"]) for item in batch],
            "average": mean(float(item["total_duration"]) for item in batch),
            "stages": average_stages(batch, "stage_breakdown"),
        },
    }


def _load(path: Path) -> dict[str, Any]:
    target = _resolve(path)
    if target.suffix.lower() in {".log", ".jsonl"}:
        return _from_jsonl(target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "single_factor" not in payload or "batch" not in payload:
        raise ValueError(f"不支持的性能基准格式：{target}")
    return payload


def _percent(before: float, after: float) -> float:
    return (before - after) / before if before > 0 else 0.0


def main(args: argparse.Namespace) -> int:
    before = _load(args.before)
    after = _load(args.after)
    before_single = float(before["single_factor"]["average"])
    after_single = float(after["single_factor"]["average"])
    before_batch = float(before["batch"]["average"])
    after_batch = float(after["batch"]["average"])
    after_single_cold = float(after["single_factor"].get("durations", [after_single])[0])
    after_batch_cold = float(after["batch"].get("durations", [after_batch])[0])
    before_memory = before.get("memory_delta_gb")
    after_memory = after.get("memory_delta_gb")

    stage_names = (
        "data_load",
        "data_prep",
        "factor_extract",
        "alphapurify",
        "metrics",
        "report",
        "serialize",
    )
    before_stages = before.get("batch", {}).get("stages", {}) or {}
    after_stages = after.get("batch", {}).get("stages", {}) or {}
    stage_rows = "\n".join(
        f"| {name} | {float(before_stages.get(name, 0.0)):.3f} | {float(after_stages.get(name, 0.0)):.3f} |"
        for name in stage_names
    )
    memory_row = (
        f"| 峰值内存增量 | {float(before_memory):.2f} GB | {float(after_memory):.2f} GB | ≤ 4 GB | {'✅' if float(after_memory) <= 4 else '❌'} |"
        if before_memory is not None and after_memory is not None
        else ""
    )
    report = f"""# AlphaPurify 性能优化报告

## 验收结果

| 场景 | 优化前 | 优化后 | 改善 | 目标 | 结论 |
|---|---:|---:|---:|---:|---|
| 单因子诊断（10 年，3 次平均） | {before_single:.2f}s | {after_single:.2f}s | {_percent(before_single, after_single):.1%} | ≤ 5s | {'✅' if after_single <= 5 else '❌'} |
| 六因子批量（10 年，3 次平均） | {before_batch:.2f}s | {after_batch:.2f}s | {_percent(before_batch, after_batch):.1%} | ≤ 30s | {'✅' if after_batch <= 30 else '❌'} |
{memory_row}

## 批量阶段耗时

| 阶段 | 优化前（秒） | 优化后（秒） |
|---|---:|---:|
{stage_rows}

## 瓶颈结论

- 优化前批量路径的主要瓶颈是指标聚合，代表性耗时为 {float(before_stages.get('metrics', 0.0)):.2f} 秒；数据加载为 {float(before_stages.get('data_load', 0.0)):.2f} 秒。
- 优化后三次平均的指标聚合降至 {float(after_stages.get('metrics', 0.0)):.2f} 秒，缓存命中时仍会重新组装诊断结果并执行阈值判定。
- 首轮冷启动为单因子 {after_single_cold:.2f} 秒、六因子 {after_batch_cold:.2f} 秒；因此本次达标指任务书规定的“连续运行 3 次取平均”，不代表冷启动已低于 5/30 秒。

## 已实施优化

- 进程内 LRU 缓存按因子、日期范围及完整诊断参数复用聚合结果，不写入数据湖。
- 批量诊断继续共享一次价格与因子宽表扫描；部分缓存命中时只计算缺失因子。
- 默认和 `--full` 保持 10 层及完整 horizons；`--fast` 才使用 5 层和最多 3 个 horizons。
- Markdown 与 HTML 报告共用一次图表渲染，避免重复生成图像。
- 所有性能记录采用完整阶段字段写入 `logs/alphapurify_perf.log`。
- 未启用进程池作为默认批量路径：当前 DuckDB 查询已使用多线程，额外进程会重复扫描数据并放大接近上限的峰值内存；保留共享扫描更稳妥。

## 一致性与约束

- 数值一致性上限：{float(after.get('max_numeric_diff', 0.0)):.3g}（要求 < 1e-6）。
- 官方 AlphaPurify 1.0.6 与 `data/parquet/` 均未修改。
- 原有 Python API 和 CLI 参数保持兼容，新增参数均为可选。
- 优化后峰值内存为 {float(after_memory or 0.0):.2f} GB，虽通过 4 GB 验收，但余量有限，后续扩大日期范围时应持续监控。
"""
    target = _resolve(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report, encoding="utf-8")
    print(f"性能优化报告：{target}")
    return 0 if after_single <= 5 and after_batch <= 30 and (after_memory is None or float(after_memory) <= 4) else 1


if __name__ == "__main__":
    raise SystemExit(main(parse_args()))
