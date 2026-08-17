# -*- coding: utf-8 -*-
"""联合回测：默认 vs 仅改 beta_min_low_frac + mv_tier_small_max_w。"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
for p in (ROOT, SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from constraint_param_sweep import (  # noqa: E402
    SIMPLIFIED_BASELINE,
    _apply_overrides,
    _fmt_pct,
    _run_rolling_local,
    _summarize_rolling,
)
from constraint_ablation import _sample_windows  # noqa: E402

OUTPUT_DIR = ROOT / "output" / "dividend_lowvol"
LOG_PATH = OUTPUT_DIR / "compare_two_params.log"
REPORT_MD = OUTPUT_DIR / "compare_two_params.md"
REPORT_JSON = OUTPUT_DIR / "compare_two_params.json"

SCENARIOS = {
    "default": {
        "label": "旧默认（small_max_w=30%, beta_min_low=35%）",
        "overrides": {
            "DLV_MV_TIER_SMALL_MAX_WEIGHT": "0.3",
            "DLV_BETA_MIN_LOW_FRAC": "0.35",
        },
    },
    "tuned": {
        "label": "新默认（small_max_w=40%, beta_min_low=45%）",
        "overrides": {},
    },
}


def _setup_logger() -> logging.Logger:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("compare_two_params")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8", mode="w")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def _write_report(rows: list[dict], logger: logging.Logger) -> None:
    base = next(r for r in rows if r["scenario_id"] == "default")
    tuned = next(r for r in rows if r["scenario_id"] == "tuned")
    lines = [
        "# 联合回测：仅改 2 参数",
        "",
        f"> 生成：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "配置：消融精简基线（市值分层 + 行业上限 + Beta 分散），12 抽样窗口。",
        "改动：`DLV_MV_TIER_SMALL_MAX_WEIGHT` 30%→40%，`DLV_BETA_MIN_LOW_FRAC` 35%→45%。",
        "",
        "| 场景 | 年化均值 | 平均回撤 | 最差回撤 | 满窗口 | trades均值 |",
        "|------|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['label']} | {_fmt_pct(r.get('cagr_mean'))} | "
            f"{_fmt_pct(r.get('dd_mean'))} | {_fmt_pct(r.get('dd_worst'))} | "
            f"{int(r.get('complete_count') or 0)}/{int(r.get('window_count') or 0)} | "
            f"{r.get('trades_mean', 0):.0f} |"
        )
    dc = (tuned.get("cagr_mean") or 0) - (base.get("cagr_mean") or 0)
    dd = (tuned.get("dd_mean") or 0) - (base.get("dd_mean") or 0)
    dw = (tuned.get("dd_worst") or 0) - (base.get("dd_worst") or 0)
    lines.extend(
        [
            "",
            "## 联合 Δ（tuned − default）",
            "",
            f"- Δ年化：**{dc:+.2f}pp**",
            f"- Δ平均回撤：**{dd:+.2f}pp**",
            f"- Δ最差回撤：**{dw:+.2f}pp**",
            "",
            "## OAT 单因子加总（参考）",
            "",
            "- small_max_w 40%：+0.15pp 年化，+1.37pp 回撤",
            "- beta_min_low 45%：+0.94pp 年化，+0.10pp 回撤",
            "- 加总上界：+0.87pp 年化，+1.56pp 回撤",
            "",
        ]
    )
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    REPORT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("报告已写入 %s", REPORT_MD)


def main(argv: list[str] | None = None) -> int:
    from market_data import configure_stdout_utf8
    from monthly_rolling_backtest import WINDOW_YEARS, monthly_windows

    configure_stdout_utf8()
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-windows", type=int, default=12)
    args = parser.parse_args(argv)

    logger = _setup_logger()
    windows = _sample_windows(monthly_windows(), args.sample_windows)
    logger.info("联合回测开始：%d 窗口 × %d 场景", len(windows), len(SCENARIOS))

    rows: list[dict] = []
    t0 = time.perf_counter()
    for sid, spec in SCENARIOS.items():
        logger.info("--- %s ---", spec["label"])
        _apply_overrides(spec["overrides"])
        _, summary, elapsed = _run_rolling_local(
            windows,
            window_years=WINDOW_YEARS,
            verbose=False,
            logger=logger,
            scenario_id=sid,
        )
        row = {"scenario_id": sid, "label": spec["label"], "elapsed_sec": round(elapsed, 1), **summary}
        rows.append(row)
        logger.info(
            "汇总 %s: 年化=%s 回撤=%s 最差=%s (%ds)",
            sid,
            _fmt_pct(summary.get("cagr_mean")),
            _fmt_pct(summary.get("dd_mean")),
            _fmt_pct(summary.get("dd_worst")),
            int(elapsed),
        )

    _write_report(rows, logger)
    logger.info("总耗时 %.1f 分钟", (time.perf_counter() - t0) / 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
