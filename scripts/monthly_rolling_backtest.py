# -*- coding: utf-8 -*-
"""从指定月份起，每月一组固定 N 年窗口的滚动日历回测。"""
from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUTPUT_DIR = ROOT / "output" / "dividend_lowvol"
FIRST_START = pd.Timestamp(os.environ.get("DLV_MONTHLY_ROLLING_START", "2015-01-15"))
WINDOW_YEARS = float(os.environ.get("DLV_MONTHLY_ROLLING_YEARS", "5"))
MAX_END = pd.Timestamp(os.environ.get("DLV_MONTHLY_ROLLING_MAX_END", "2026-07-01"))
CAGR_STD_THRESHOLD = float(os.environ.get("DLV_ROLLING_CAGR_STD_THRESHOLD", "4.0"))
OUT_STEM = os.environ.get("DLV_MONTHLY_ROLLING_OUT", "monthly_rolling_5y")


def monthly_windows(
    *,
    first_start: pd.Timestamp = FIRST_START,
    years: float = WINDOW_YEARS,
    max_end: pd.Timestamp = MAX_END,
) -> list[tuple[str, str, str]]:
    """每月同步滚动起止日，窗口时长恒定。"""
    rows: list[tuple[str, str, str]] = []
    cur = first_start
    idx = 0
    while True:
        end = pd.Timestamp(year=int(cur.year + years), month=cur.month, day=1)
        if end > max_end:
            break
        idx += 1
        label = f"M{idx:03d} {cur.date()}~{end.date()}"
        rows.append((label, cur.date().isoformat(), end.date().isoformat()))
        cur += pd.DateOffset(months=1)
    return rows


def _fmt_pct(v: float | None, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:.{digits}f}%"


def run_all(*, verbose: bool = True) -> tuple[pd.DataFrame, dict]:
    windows = monthly_windows()
    if not windows:
        raise SystemExit("无可用窗口：请检查 DLV_MONTHLY_ROLLING_START / MAX_END / YEARS")

    union_start = min(w[1] for w in windows)
    union_end = max(w[2] for w in windows)

    if verbose:
        print(
            f"月度滚动：{len(windows)} 组 × {WINDOW_YEARS:g} 年"
            f"（{union_start} ~ {union_end}）…",
            flush=True,
        )

    from dividend_lowvol_rotation.backtest import prepare_backtest_context, run_backtest

    t0 = time.perf_counter()
    ctx = prepare_backtest_context(union_start, union_end, verbose=verbose)
    if verbose:
        print(f"上下文就绪 {time.perf_counter() - t0:.0f}s\n", flush=True)

    results: list[dict] = []
    for idx, (label, start, end) in enumerate(windows, 1):
        if verbose:
            print(f"[{idx}/{len(windows)}] {label} …", flush=True)
        t1 = time.perf_counter()
        _, trades, _, _, meta, _ = run_backtest(
            start=start,
            end=end,
            ctx=ctx,
            verbose=False,
            record_details=False,
        )
        sec = time.perf_counter() - t1
        years = (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25
        row = {
            "window": label,
            "start": start,
            "end": end,
            "years": round(years, 3),
            "total_return_pct": meta.get("total_return_pct"),
            "cagr_pct": meta.get("cagr_pct"),
            "max_drawdown_pct": meta.get("max_drawdown_pct"),
            "sharpe": meta.get("sharpe"),
            "trades": meta.get("trade_count") or len(trades),
            "final_nav": meta.get("final_nav"),
            "sec": round(sec, 1),
        }
        results.append(row)
        if verbose:
            print(
                f"  ret={row['total_return_pct']:.1f}% "
                f"CAGR={row['cagr_pct']:.1f}% "
                f"maxDD={row['max_drawdown_pct']:.1f}% "
                f"trades={row['trades']} ({sec:.0f}s)",
                flush=True,
            )

    df = pd.DataFrame(results)
    cagrs = df["cagr_pct"].dropna().astype(float)
    dds = df["max_drawdown_pct"].dropna().astype(float)
    summary = {
        "window_count": len(df),
        "window_years": WINDOW_YEARS,
        "first_start": FIRST_START.date().isoformat(),
        "cagr_mean": float(cagrs.mean()) if len(cagrs) else None,
        "cagr_std": float(cagrs.std(ddof=0)) if len(cagrs) else None,
        "cagr_min": float(cagrs.min()) if len(cagrs) else None,
        "cagr_max": float(cagrs.max()) if len(cagrs) else None,
        "dd_mean": float(dds.mean()) if len(dds) else None,
        "dd_worst": float(dds.min()) if len(dds) else None,
        "cagr_std_threshold": CAGR_STD_THRESHOLD,
        "all_weather": bool(len(cagrs) and float(cagrs.std(ddof=0)) < CAGR_STD_THRESHOLD),
        "union_start": union_start,
        "union_end": union_end,
    }
    return df, summary


def render_report(df: pd.DataFrame, summary: dict) -> str:
    lines = [
        f"# 月度滚动回测（固定 {summary['window_years']:g} 年窗口）",
        "",
        f"> 自 **{summary['first_start']}** 起每月一组，起止同步滚动，时长恒定。",
        "",
        f"- 窗口数：**{summary['window_count']}**",
        f"- 联合区间：{summary['union_start']} ~ {summary['union_end']}",
        f"- 年化均值：**{_fmt_pct(summary['cagr_mean'])}**",
        f"- 年化标准差：**{_fmt_pct(summary['cagr_std'])}**（阈值 < {_fmt_pct(summary['cagr_std_threshold'])}）",
        f"- 年化区间：[{_fmt_pct(summary['cagr_min'])}, {_fmt_pct(summary['cagr_max'])}]",
        f"- 平均最大回撤：**{_fmt_pct(summary['dd_mean'])}** · 最差 **{_fmt_pct(summary['dd_worst'])}**",
        "",
        "## 判定",
        "",
    ]
    if summary["all_weather"]:
        lines.append(
            f"✅ **全天候底仓特征**：{summary['window_count']} 组年化标准差 "
            f"{_fmt_pct(summary['cagr_std'])} < {_fmt_pct(summary['cagr_std_threshold'])}。"
        )
    else:
        lines.append(
            f"⚠️ 年化标准差 {_fmt_pct(summary['cagr_std'])} ≥ "
            f"{_fmt_pct(summary['cagr_std_threshold'])}，起点月份对收益仍有可见影响。"
        )

    # 按起点年份汇总
    df2 = df.copy()
    df2["start_year"] = pd.to_datetime(df2["start"]).dt.year
    by_year = (
        df2.groupby("start_year")["cagr_pct"]
        .agg(["count", "mean", "std", "min", "max"])
        .round(2)
    )
    lines.extend(["", "## 按起点年份汇总（年化 %）", ""])
    lines.append("| 起点年 | 窗口数 | 均值 | 标准差 | 最小 | 最大 |")
    lines.append("|------|---:|---:|---:|---:|---:|")
    for year, r in by_year.iterrows():
        std_s = f"{r['std']:.2f}" if pd.notna(r["std"]) else "—"
        lines.append(
            f"| {int(year)} | {int(r['count'])} | {r['mean']:.2f} | {std_s} | "
            f"{r['min']:.2f} | {r['max']:.2f} |"
        )

    lines.extend(
        [
            "",
            "## 各窗口结果",
            "",
            "| 窗口 | 区间 | 时长(年) | 总收益 | 年化 | 最大回撤 | Sharpe | 成交 |",
            "|------|------|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for _, r in df.iterrows():
        sharpe = r.get("sharpe")
        sharpe_s = f"{sharpe:.2f}" if pd.notna(sharpe) else "—"
        lines.append(
            f"| {r['window']} | {r['start']}~{r['end']} | {r['years']:.2f} | "
            f"{_fmt_pct(r['total_return_pct'])} | {_fmt_pct(r['cagr_pct'])} | "
            f"{_fmt_pct(r['max_drawdown_pct'])} | {sharpe_s} | {int(r['trades'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    from market_data import configure_stdout_utf8

    configure_stdout_utf8()
    df, summary = run_all(verbose=True)
    report = render_report(df, summary)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_md = OUTPUT_DIR / f"{OUT_STEM}.md"
    out_csv = OUTPUT_DIR / f"{OUT_STEM}.csv"
    out_md.write_text(report, encoding="utf-8")
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")

    print("\n" + report)
    print(f"\n已写入：\n  {out_md}\n  {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
