# -*- coding: utf-8 -*-
"""保留约束的参数扫描：单因子逐值调参（收益 + 回撤双指标）。"""
from __future__ import annotations

import argparse
import json
import logging
import os
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

from constraint_ablation import (  # noqa: E402
    ENV_KEYS,
    _reload_strategy_modules,
    _sample_windows,
)

OUTPUT_DIR = ROOT / "output" / "dividend_lowvol"
LOG_PATH = OUTPUT_DIR / "constraint_param_sweep.log"
REPORT_MD = OUTPUT_DIR / "constraint_param_sweep.md"
REPORT_JSON = OUTPUT_DIR / "constraint_param_sweep.json"
PARTIAL_CSV = OUTPUT_DIR / "constraint_param_sweep_partial.csv"

# 当前精简配置基线（代码已删除 17 项无效因子，无需 env 覆盖）
SIMPLIFIED_BASELINE: dict[str, str] = {}

# 单因子扫描：env → 候选值（含当前默认）
PARAM_GROUPS: dict[str, dict] = {
    "mv_tier_large_yi": {
        "label": "市值大盘线（亿）",
        "env": "DLV_MV_TIER_LARGE_CNY",
        "values": [100, 150, 200, 300, 500],
        "default": 200,
        "fmt": lambda v: str(int(v * 1e8)),
        "display": lambda v: f"{int(v)}亿",
    },
    "mv_tier_small_max_w": {
        "label": "中小盘仓位上限",
        "env": "DLV_MV_TIER_SMALL_MAX_WEIGHT",
        "values": [0.10, 0.20, 0.30, 0.40, 0.50],
        "default": 0.40,
        "fmt": str,
        "display": lambda v: f"{v:.0%}",
    },
    "max_industry_weight": {
        "label": "单行业持仓上限",
        "env": "DLV_MAX_INDUSTRY_WEIGHT",
        "values": [0.15, 0.20, 0.25, 0.30, 0.35],
        "default": 0.20,
        "fmt": str,
        "display": lambda v: f"{v:.0%}",
    },
    "max_defensive_weight": {
        "label": "防御行业合计上限",
        "env": "DLV_MAX_DEFENSIVE_INDUSTRY_WEIGHT",
        "values": [0.35, 0.40, 0.45, 0.50, 0.55],
        "default": 0.45,
        "fmt": str,
        "display": lambda v: f"{v:.0%}",
    },
    "max_top3_weight": {
        "label": "前三行业合计上限",
        "env": "DLV_MAX_TOP3_INDUSTRY_WEIGHT",
        "values": [0.40, 0.45, 0.50, 0.55, 0.60],
        "default": 0.50,
        "fmt": str,
        "display": lambda v: f"{v:.0%}",
    },
    "beta_low_threshold": {
        "label": "低 Beta 阈值",
        "env": "DLV_BETA_LOW_THRESHOLD",
        "values": [0.55, 0.68, 0.75, 0.85],
        "default": 0.68,
        "fmt": str,
        "display": lambda v: f"{v:.2f}",
    },
    "beta_min_low_frac": {
        "label": "低 Beta 最低占比",
        "env": "DLV_BETA_MIN_LOW_FRAC",
        "values": [0.25, 0.35, 0.45, 0.55],
        "default": 0.45,
        "fmt": str,
        "display": lambda v: f"{v:.0%}",
    },
    "beta_max_high_frac": {
        "label": "高 Beta 最高占比",
        "env": "DLV_BETA_MAX_HIGH_FRAC",
        "values": [0.70, 0.81, 0.90, 1.00],
        "default": 0.81,
        "fmt": str,
        "display": lambda v: f"{v:.0%}",
    },
}


def _setup_logger() -> logging.Logger:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("constraint_param_sweep")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8", mode="a")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


PARAM_ENV_KEYS = tuple(spec["env"] for spec in PARAM_GROUPS.values())

ALL_SWEEP_ENV_KEYS = tuple(
    dict.fromkeys([*SIMPLIFIED_BASELINE.keys(), *PARAM_ENV_KEYS, *ENV_KEYS])
)


def _summarize_rolling(df: pd.DataFrame, window_years: float) -> dict:
    from constraint_ablation import _summarize_rolling as _base

    return _base(df, window_years)


def _fmt_pct(val, *, suffix: str = "%") -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "—"
    return f"{float(val):.2f}{suffix}"


def _run_rolling_local(
    windows: list[tuple[str, str, str]],
    *,
    window_years: float,
    verbose: bool,
    logger: logging.Logger,
    scenario_id: str,
) -> tuple[pd.DataFrame, dict, float]:
    from dividend_lowvol_rotation.backtest import prepare_backtest_context, run_backtest

    union_start = min(w[1] for w in windows)
    union_end = max(w[2] for w in windows)
    t0 = time.perf_counter()
    logger.info("[%s] 预加载上下文 %s ~ %s …", scenario_id, union_start, union_end)
    ctx = prepare_backtest_context(union_start, union_end, verbose=verbose)
    prep_sec = time.perf_counter() - t0
    logger.info("[%s] 上下文就绪 %.0fs，开始 %d 窗口", scenario_id, prep_sec, len(windows))

    rows: list[dict] = []
    for idx, (label, start, end) in enumerate(windows, 1):
        t1 = time.perf_counter()
        logger.info("[%s] 窗口 %d/%d %s", scenario_id, idx, len(windows), label)
        nav_df, trades, _, _, meta, _ = run_backtest(
            start=start,
            end=end,
            ctx=ctx,
            verbose=False,
            record_details=False,
        )
        sec = time.perf_counter() - t1
        nominal_years = (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25
        if not nav_df.empty:
            effective_years = (
                pd.Timestamp(nav_df["date"].iloc[-1]) - pd.Timestamp(nav_df["date"].iloc[0])
            ).days / 365.25
        else:
            effective_years = nominal_years
        complete = effective_years >= window_years * 0.95
        rows.append(
            {
                "window": label,
                "start": start,
                "end": end,
                "complete": complete,
                "cagr_pct": meta.get("cagr_pct"),
                "max_drawdown_pct": meta.get("max_drawdown_pct"),
                "trades": meta.get("trade_count") or len(trades),
                "sec": round(sec, 1),
            }
        )
        logger.info(
            "[%s]   CAGR=%.2f%% maxDD=%.2f%% trades=%s (%.0fs)",
            scenario_id,
            meta.get("cagr_pct") or 0,
            meta.get("max_drawdown_pct") or 0,
            meta.get("trade_count") or len(trades),
            sec,
        )
        if idx == 1 and (sec < 45 or (meta.get("trade_count") or len(trades)) < 30):
            msg = (
                f"首窗口异常 sec={sec:.0f}s trades={meta.get('trade_count') or len(trades)}，"
                "疑似软评分/缓存失效，中止本场景"
            )
            logger.error("[%s] %s", scenario_id, msg)
            raise RuntimeError(msg)
    df = pd.DataFrame(rows)
    summary = _summarize_rolling(df, window_years)
    elapsed = time.perf_counter() - t0
    return df, summary, elapsed


def _apply_overrides(overrides: dict[str, str]) -> None:
    import duckdb_cache

    for key in ALL_SWEEP_ENV_KEYS:
        os.environ.pop(key, None)
    merged = dict(SIMPLIFIED_BASELINE)
    merged.update(overrides)
    for key, val in merged.items():
        if key.startswith("DLV_"):
            os.environ[key] = val
    duckdb_cache._DUCKDB_READY = None
    _reload_strategy_modules()


def _rank_series(values: pd.Series, *, higher_better: bool) -> pd.Series:
    return values.rank(ascending=not higher_better, method="min")


def _pick_best_in_group(df: pd.DataFrame) -> dict | None:
    work = df.dropna(subset=["cagr_mean", "dd_mean", "dd_worst"]).copy()
    if work.empty:
        return None
    work["rank_cagr"] = _rank_series(work["cagr_mean"], higher_better=True)
    work["rank_dd"] = _rank_series(work["dd_mean"], higher_better=True)
    work["rank_dw"] = _rank_series(work["dd_worst"], higher_better=True)
    work["composite_rank"] = (work["rank_cagr"] + work["rank_dd"] + work["rank_dw"]) / 3.0
    best = work.loc[work["composite_rank"].idxmin()]
    return best.to_dict()


def _write_report(rows: list[dict], logger: logging.Logger) -> None:
    df = pd.DataFrame(rows)
    lines = [
        "# 保留约束参数扫描（单因子逐值）",
        "",
        f"> 生成：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "基准：消融精简后配置（仅市值分层 + 行业上限 + Beta 分散）。",
        "优选：年化↑、平均回撤↑、最差回撤↑ 三指标等权排名。",
        "",
    ]
    recommendations: list[str] = []
    for gid, spec in PARAM_GROUPS.items():
        sub = df[df["param_group"] == gid].copy()
        if sub.empty:
            continue
        lines.append(f"## {spec['label']}（`{gid}`）")
        lines.append("")
        lines.append("| 取值 | 年化均值 | 平均回撤 | 最差回撤 | 满窗口数 | 备注 |")
        lines.append("|------|---:|---:|---:|---:|------|")
        best = _pick_best_in_group(sub)
        for _, r in sub.iterrows():
            note = ""
            if r.get("error"):
                note = f"失败: {r['error']}"
            elif r.get("metric_scope") == "all":
                note = "仅全窗口统计"
            if r.get("is_default"):
                note = (note + "；当前默认").strip("；")
            if best is not None and r.get("scenario_id") == best.get("scenario_id"):
                note = (note + "；**推荐**").strip("；")
            lines.append(
                f"| {r['value_display']} | {_fmt_pct(r.get('cagr_mean'))} | "
                f"{_fmt_pct(r.get('dd_mean'))} | {_fmt_pct(r.get('dd_worst'))} | "
                f"{int(r.get('complete_count') or 0)}/{int(r.get('window_count') or 0)} | "
                f"{note or '—'} |"
            )
        if best is not None:
            recommendations.append(
                f"- **{spec['label']}**：`{best['value_display']}` "
                f"（年化 {_fmt_pct(best.get('cagr_mean'))}，回撤 {_fmt_pct(best.get('dd_mean'))}）"
            )
        lines.append("")
    if recommendations:
        lines.extend(["## 推荐取值", ""] + recommendations + [""])
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    REPORT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("报告已写入 %s", REPORT_MD)


def _build_tasks(groups: list[str]) -> list[dict]:
    tasks: list[dict] = []
    for gid in groups:
        spec = PARAM_GROUPS[gid]
        for val in spec["values"]:
            sid = f"{gid}={spec['display'](val)}"
            tasks.append(
                {
                    "scenario_id": sid,
                    "param_group": gid,
                    "param_label": spec["label"],
                    "value_raw": val,
                    "value_display": spec["display"](val),
                    "is_default": val == spec["default"],
                    "overrides": {spec["env"]: spec["fmt"](val)},
                }
            )
    return tasks


def main(argv: list[str] | None = None) -> int:
    from market_data import configure_stdout_utf8
    from monthly_rolling_backtest import WINDOW_YEARS, monthly_windows

    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="保留约束参数扫描")
    parser.add_argument("--sample-windows", type=int, default=12)
    parser.add_argument(
        "--params",
        nargs="*",
        default=None,
        help=f"参数组 id，默认全部：{', '.join(PARAM_GROUPS)}",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="仅从 partial CSV 重新生成报告",
    )
    args = parser.parse_args(argv)

    logger = _setup_logger()

    if args.report_only:
        if not PARTIAL_CSV.exists():
            raise SystemExit(f"缺少 {PARTIAL_CSV}")
        rows = pd.read_csv(PARTIAL_CSV).to_dict(orient="records")
        _write_report(rows, logger)
        return 0

    groups = args.params or list(PARAM_GROUPS.keys())
    for g in groups:
        if g not in PARAM_GROUPS:
            raise SystemExit(f"未知参数组: {g}")

    tasks = _build_tasks(groups)
    all_w = monthly_windows()
    windows = _sample_windows(all_w, args.sample_windows)

    est_per = 165.0
    est_prep = 150.0
    est_min = len(tasks) * (est_prep + len(windows) * est_per) / 60
    logger.info("=" * 60)
    logger.info(
        "参数扫描开始：%d 组 × 共 %d 取值点，%d 窗口，预估 ≈ %.0f 分钟",
        len(groups),
        len(tasks),
        len(windows),
        est_min,
    )

    rows: list[dict] = []
    t0 = time.perf_counter()
    for i, task in enumerate(tasks, 1):
        logger.info(
            "--- %d/%d [%s] %s = %s ---",
            i,
            len(tasks),
            task["param_group"],
            task["param_label"],
            task["value_display"],
        )
        _apply_overrides(task["overrides"])
        try:
            _, summary, elapsed = _run_rolling_local(
                windows,
                window_years=WINDOW_YEARS,
                verbose=args.verbose,
                logger=logger,
                scenario_id=task["scenario_id"],
            )
            row = {
                **task,
                "elapsed_sec": round(elapsed, 1),
                **summary,
            }
            rows.append(row)
            pd.DataFrame(rows).to_csv(PARTIAL_CSV, index=False, encoding="utf-8-sig")
        except Exception as exc:
            logger.exception("失败 %s: %s", task["scenario_id"], exc)
            rows.append({**task, "error": str(exc)})

    _write_report(rows, logger)
    logger.info("总耗时 %.0f 分钟", (time.perf_counter() - t0) / 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
