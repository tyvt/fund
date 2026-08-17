# -*- coding: utf-8 -*-
"""因子/约束消融：抽样滚动窗口对比 baseline，识别可删项。"""
from __future__ import annotations

import argparse
import importlib
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

OUTPUT_DIR = ROOT / "output" / "dividend_lowvol"
LOG_PATH = OUTPUT_DIR / "constraint_ablation.log"
REPORT_MD = OUTPUT_DIR / "constraint_ablation.md"
REPORT_JSON = OUTPUT_DIR / "constraint_ablation.json"

# 消融时允许覆盖的环境变量（仅保留 3 项结构性约束）
ENV_KEYS = (
    "DLV_MV_TIER_CAP_ENABLED",
    "DLV_INDUSTRY_CAP_ENABLED",
    "DLV_BETA_BALANCE_ENABLED",
)

RELOAD_MODULES = (
    "dividend_lowvol_rotation.config",
    "dividend_lowvol_rotation.market_cap",
    "dividend_lowvol_rotation.enhanced_factors",
    "dividend_lowvol_rotation.index_portfolio",
    "dividend_lowvol_rotation.industry_caps",
    "dividend_lowvol_rotation.scoring",
    "dividend_lowvol_rotation.backtest",
)

# 历史完整消融（21 场景含 17 项已删因子）见 output/dividend_lowvol/constraint_ablation.md
SCENARIOS: dict[str, dict[str, str]] = {
    "baseline": {"label": "当前默认（3 项约束 + 排雷硬过滤）"},
    "w/o_mv_tier": {
        "label": "关闭：市值分层仓位上限",
        "DLV_MV_TIER_CAP_ENABLED": "false",
    },
    "w/o_industry_cap": {
        "label": "关闭：行业分散上限",
        "DLV_INDUSTRY_CAP_ENABLED": "false",
    },
    "w/o_beta": {
        "label": "关闭：Beta 分散",
        "DLV_BETA_BALANCE_ENABLED": "false",
    },
}

# 判定阈值（相对 baseline，关闭该因子后的变化）
CAGR_NEUTRAL_PP = 0.08
DD_NEUTRAL_PP = 0.5


def _setup_logger() -> logging.Logger:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("constraint_ablation")
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


def _reload_strategy_modules() -> None:
    import duckdb_cache

    duckdb_cache._DUCKDB_READY = None
    for name in RELOAD_MODULES:
        mod = importlib.import_module(name)
        importlib.reload(mod)


def _apply_scenario(overrides: dict[str, str]) -> None:
    for key in ENV_KEYS:
        os.environ.pop(key, None)
    for key, val in overrides.items():
        if key.startswith("DLV_"):
            os.environ[key] = val
    _reload_strategy_modules()


def _sample_windows(all_windows: list, sample_n: int) -> list:
    if sample_n <= 0 or sample_n >= len(all_windows):
        return all_windows
    idxs = [int(round(i * (len(all_windows) - 1) / (sample_n - 1))) for i in range(sample_n)]
    return [all_windows[i] for i in dict.fromkeys(idxs)]


def _summarize_rolling(df: pd.DataFrame, window_years: float) -> dict:
    complete = df[df["complete"]] if "complete" in df.columns else df
    scope = "complete"
    cagrs = complete["cagr_pct"].dropna().astype(float)
    dds = complete["max_drawdown_pct"].dropna().astype(float)
    if len(cagrs) == 0:
        scope = "all"
        cagrs = df["cagr_pct"].dropna().astype(float)
        dds = df["max_drawdown_pct"].dropna().astype(float)
    return {
        "window_count": len(df),
        "complete_count": int(len(complete)),
        "metric_scope": scope,
        "cagr_mean": float(cagrs.mean()) if len(cagrs) else None,
        "cagr_std": float(cagrs.std(ddof=0)) if len(cagrs) else None,
        "dd_mean": float(dds.mean()) if len(dds) else None,
        "dd_worst": float(dds.min()) if len(dds) else None,
        "trades_mean": float(df["trades"].mean()) if "trades" in df.columns else None,
        "window_years": window_years,
    }


def _run_rolling(
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
    df = pd.DataFrame(rows)
    summary = _summarize_rolling(df, window_years)
    elapsed = time.perf_counter() - t0
    return df, summary, elapsed


def _classify(baseline: dict, row: dict) -> str:
    if row.get("id") == "baseline":
        return "baseline"
    bc = baseline.get("cagr_mean")
    bd = baseline.get("dd_mean")
    bdw = baseline.get("dd_worst")
    c = row.get("cagr_mean")
    d = row.get("dd_mean")
    dw = row.get("dd_worst")
    if bc is None or c is None:
        return "unknown"
    d_cagr = float(c) - float(bc)
    d_dd = float(d) - float(bd) if d is not None and bd is not None else 0.0
    d_dw = float(dw) - float(bdw) if dw is not None and bdw is not None else 0.0
    cagr_ok = d_cagr >= -CAGR_NEUTRAL_PP
    dd_ok = d_dd >= -DD_NEUTRAL_PP
    dw_ok = d_dw >= -DD_NEUTRAL_PP
    # 收益与回撤（含最差窗口）均需不差于阈值，方可删除
    if cagr_ok and dd_ok and dw_ok:
        return "removable"
    if not cagr_ok and not dd_ok:
        return "harmful_both"
    if not dd_ok or not dw_ok:
        return "harmful_dd"
    if not cagr_ok:
        return "harmful_cagr"
    if d_cagr > CAGR_NEUTRAL_PP and dd_ok:
        return "beneficial"
    return "mixed"


def _verdict_label(verdict: str) -> str:
    return {
        "baseline": "基准",
        "removable": "**可删**",
        "harmful_remove": "删则变差",
        "harmful_both": "删则收益/回撤均变差",
        "harmful_cagr": "删则收益变差",
        "harmful_dd": "删则回撤变差",
        "beneficial": "有益",
        "mixed": "混合",
        "unknown": "—",
        "error": "错误",
    }.get(verdict, verdict)


def _write_report(rows: list[dict], removable: list[dict], logger: logging.Logger) -> None:
    lines = [
        "# 因子/约束消融（滚动窗口）",
        "",
        f"> 生成：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "判定规则：关闭后相对基准，**年化**不差于 -0.08pp **且** **平均回撤/最差回撤** 不差于 -0.5pp → 可删。",
        "",
        "| 场景 | 年化均值 | Δ年化 | 平均回撤 | Δ回撤 | 最差回撤 | Δ最差 | 判定 |",
        "|------|---:|---:|---:|---:|---:|---:|------|",
    ]
    base_cagr = next((r["cagr_mean"] for r in rows if r["id"] == "baseline"), None)
    base_dd = next((r["dd_mean"] for r in rows if r["id"] == "baseline"), None)
    base_dw = next((r["dd_worst"] for r in rows if r["id"] == "baseline"), None)
    keep_rows: list[dict] = []
    for r in rows:
        dc = (
            f"{float(r['cagr_mean']) - float(base_cagr):+.2f}"
            if base_cagr is not None and r.get("cagr_mean") is not None
            else "—"
        )
        dd = (
            f"{float(r['dd_mean']) - float(base_dd):+.2f}"
            if base_dd is not None and r.get("dd_mean") is not None
            else "—"
        )
        dw = (
            f"{float(r['dd_worst']) - float(base_dw):+.2f}"
            if base_dw is not None and r.get("dd_worst") is not None
            else "—"
        )
        verdict = _verdict_label(r.get("verdict", ""))
        lines.append(
            f"| {r['label']} | {r.get('cagr_mean', 0):.2f}% | {dc} | "
            f"{r.get('dd_mean', 0):.2f}% | {dd} | {r.get('dd_worst', 0):.2f}% | {dw} | {verdict} |"
        )
        if r.get("verdict", "").startswith("harmful"):
            keep_rows.append(r)
    if keep_rows:
        lines.extend(["", "## 必须保留（关闭后收益或回撤明显变差）", ""])
        for r in keep_rows:
            bc = float(r["cagr_mean"]) - float(base_cagr) if base_cagr else 0
            bd = float(r["dd_mean"]) - float(base_dd) if base_dd else 0
            lines.append(
                f"- **{r['label']}**（`{r['id']}`）：Δ年化 {bc:+.2f}pp，Δ平均回撤 {bd:+.2f}pp"
            )
    if removable:
        lines.extend(["", "## 建议删除（收益与回撤均无显著差异）", ""])
        for r in removable:
            lines.append(f"- **{r['label']}**（`{r['id']}`）")
    lines.append("")
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    REPORT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("报告已写入 %s", REPORT_MD)


def main(argv: list[str] | None = None) -> int:
    from market_data import configure_stdout_utf8
    from monthly_rolling_backtest import WINDOW_COUNT, WINDOW_YEARS, monthly_windows

    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="因子/约束消融")
    parser.add_argument("--sample-windows", type=int, default=12, help="抽样窗口数（0=全部79）")
    parser.add_argument("--scenarios", nargs="*", default=None, help="场景 id 子集")
    parser.add_argument("--pilot-only", action="store_true", help="仅跑 baseline 估时")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logger = _setup_logger()
    logger.info("=" * 60)
    logger.info("消融开始 sample_windows=%s", args.sample_windows)

    all_w = monthly_windows()
    windows = _sample_windows(all_w, args.sample_windows)
    ids = args.scenarios or list(SCENARIOS.keys())
    if args.pilot_only:
        ids = ["baseline"]

    est_per_window = 85.0
    est_prep = 180.0
    est_total = len(ids) * (est_prep + len(windows) * est_per_window)
    logger.info(
        "预估：%d 场景 × (%d 窗口 × ~%.0fs + 预加载 ~%.0fs) ≈ %.0f 分钟",
        len(ids),
        len(windows),
        est_per_window,
        est_prep,
        est_total / 60,
    )

    rows: list[dict] = []
    total_t0 = time.perf_counter()
    for i, sid in enumerate(ids, 1):
        if sid not in SCENARIOS:
            logger.warning("未知场景 %s", sid)
            continue
        spec = SCENARIOS[sid]
        overrides = {k: v for k, v in spec.items() if k != "label"}
        logger.info("--- 场景 %d/%d: %s ---", i, len(ids), spec["label"])
        _apply_scenario(overrides)
        try:
            _, summary, elapsed = _run_rolling(
                windows,
                window_years=WINDOW_YEARS,
                verbose=args.verbose,
                logger=logger,
                scenario_id=sid,
            )
            row = {"id": sid, "label": spec["label"], "elapsed_sec": round(elapsed, 1), **summary}
            rows.append(row)
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows).to_csv(
                OUTPUT_DIR / "constraint_ablation_partial.csv", index=False, encoding="utf-8-sig"
            )
        except Exception as exc:
            logger.exception("场景 %s 失败: %s", sid, exc)
            rows.append({"id": sid, "label": spec["label"], "error": str(exc), "verdict": "error"})

    baseline = next((r for r in rows if r.get("id") == "baseline"), rows[0] if rows else {})
    for r in rows:
        r["verdict"] = _classify(baseline, r)

    removable = [r for r in rows if r.get("verdict") == "removable"]
    _write_report(rows, removable, logger)
    logger.info("总耗时 %.0f 分钟", (time.perf_counter() - total_t0) / 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
