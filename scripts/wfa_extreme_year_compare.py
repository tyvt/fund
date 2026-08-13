# -*- coding: utf-8 -*-
"""对比优化前后（前瞻修复 + 软性评分 + 候选池保障）在极端年份的表现。"""
from __future__ import annotations

import importlib
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

START = os.environ.get("DLV_WFA_START", "2016-08-13")
END = os.environ.get("DLV_WFA_END", "2025-08-01")
EXTREME_YEARS = tuple(int(y) for y in os.environ.get("DLV_EXTREME_YEARS", "2018,2024").split(","))

BASE_ENV = {
    "DLV_INDEX_ANNUAL_REBALANCE_TIMING": "january",
    "DLV_BACKTEST_KLINE_FQ": "qfq",
}

SCENARIOS = [
    ("优化前(硬剔除)", {"DLV_SOFT_RISK_SCORING_ENABLED": "false", "DLV_SOFT_ENHANCED_SCORING_ENABLED": "false"}),
    ("优化后(默认)", {}),
]

RELOAD = (
    "dividend_lowvol_rotation.config",
    "dividend_lowvol_rotation.scoring",
    "dividend_lowvol_rotation.risk_screening",
    "dividend_lowvol_rotation.enhanced_factors",
    "dividend_lowvol_rotation.risk_regime",
    "dividend_lowvol_rotation.dynamic_params",
    "dividend_lowvol_rotation.index_portfolio",
    "dividend_lowvol_rotation.backtest",
    "dividend_lowvol_rotation.backtest_validate",
)


def reload_all() -> None:
    for name in RELOAD:
        importlib.reload(importlib.import_module(name))


def clear_dlv() -> None:
    for k in list(os.environ):
        if k.startswith("DLV_") and k not in BASE_ENV:
            os.environ.pop(k, None)


def run_label(label: str, overrides: dict[str, str], ctx) -> dict:
    clear_dlv()
    for k, v in BASE_ENV.items():
        os.environ[k] = v
    for k, v in overrides.items():
        os.environ[k] = v
    reload_all()
    from dividend_lowvol_rotation.backtest import run_backtest
    from dividend_lowvol_rotation.backtest_validate import run_wfa

    t0 = time.perf_counter()
    nav, trades, _, _, meta, _ = run_backtest(
        start=START, end=END, ctx=ctx, verbose=False, record_details=True
    )
    _, wfa, _, _, _ = run_wfa(start=START, end=END, freq="year", ctx=ctx, verbose=False)
    sec = time.perf_counter() - t0

    trades_df = trades.copy()
    if not trades_df.empty:
        trades_df["date"] = trades_df["date"].astype(str)
        trades_df["year"] = trades_df["date"].str.slice(0, 4).astype(int)

    year_stats: dict[int, dict] = {}
    if not nav.empty:
        nav = nav.copy()
        nav["date"] = nav["date"].astype(str)
        nav["year"] = nav["date"].str.slice(0, 4).astype(int)
        for y in EXTREME_YEARS:
            sub = nav[nav["year"] == y]
            if sub.empty:
                continue
            peak = sub["nav"].cummax()
            dd = (sub["nav"] / peak - 1).min() * 100
            y0 = sub["nav"].iloc[0]
            y1 = sub["nav"].iloc[-1]
            yr_ret = (y1 / y0 - 1) * 100 if y0 > 0 else None
            tr = trades_df[trades_df["year"] == y] if not trades_df.empty else trades_df
            year_stats[y] = {
                "return_pct": yr_ret,
                "max_dd_pct": float(dd),
                "trades": len(tr),
                "sells": int((tr["side"] == "卖出").sum()) if not tr.empty else 0,
            }

    return {
        "label": label,
        "total_return_pct": meta.get("total_return_pct"),
        "cagr_pct": meta.get("cagr_pct"),
        "max_drawdown_pct": meta.get("max_drawdown_pct"),
        "trades": meta.get("trade_count") or len(trades),
        "wfa_wins": wfa.strategy_wins,
        "wfa_windows": wfa.windows_active,
        "wfa_mean_edge": wfa.mean_edge_pct,
        "wfa_stitched": wfa.stitched_strategy_pct,
        "year_stats": year_stats,
        "sec": sec,
    }


def collect_screening_snapshots(ctx, overrides: dict[str, str]) -> list[dict]:
    clear_dlv()
    for k, v in BASE_ENV.items():
        os.environ[k] = v
    for k, v in overrides.items():
        os.environ[k] = v
    reload_all()
    from dividend_lowvol_rotation.rebalance_schedule import resolve_rebalance_dates
    from dividend_lowvol_rotation.scoring import run_screening
    from dividend_lowvol_rotation.dynamic_params import resolve_dynamic_params

    rows: list[dict] = []
    reb_dates = resolve_rebalance_dates(ctx.calendar, mode="index_annual")
    for rb in reb_dates:
        if rb.year not in EXTREME_YEARS:
            continue
        panel = ctx.panel_at(rb, prefetch_size=150)
        if panel.empty:
            continue
        dynamic = resolve_dynamic_params(panel, as_of=rb)
        _, _, st = run_screening(panel, as_of=rb, dynamic=dynamic)
        rows.append(
            {
                "date": rb.date().isoformat(),
                "year": rb.year,
                "pool_count": st.get("pool_count"),
                "pool_min": st.get("pool_min"),
                "pool_target": st.get("pool_target"),
                "pool_sufficient": st.get("pool_sufficient"),
                "relaxation": st.get("relaxation_level"),
                "risk_penalized": st.get("risk_penalized", st.get("risk_excluded")),
                "enhanced_penalized": st.get("enhanced_penalized", st.get("enhanced_excluded")),
            }
        )
    return rows


def main() -> int:
    for k, v in BASE_ENV.items():
        os.environ[k] = v
    reload_all()
    from dividend_lowvol_rotation.backtest import prepare_backtest_context

    print(f"预加载 ctx {START} ~ {END} …", flush=True)
    ctx = prepare_backtest_context(START, END, verbose=True)

    results: list[dict] = []
    screening: dict[str, list[dict]] = {}
    for name, ov in SCENARIOS:
        print(f"\n=== {name} ===", flush=True)
        screening[name] = collect_screening_snapshots(ctx, ov)
        r = run_label(name, ov, ctx)
        results.append(r)
        print(
            f"全段: ret={r['total_return_pct']:.1f}% CAGR={r['cagr_pct']:.1f}% "
            f"maxDD={r['max_drawdown_pct']:.1f}% trades={r['trades']} ({r['sec']:.0f}s)",
            flush=True,
        )
        print(
            f"WFA: wins={r['wfa_wins']}/{r['wfa_windows']} "
            f"mean_edge={r['wfa_mean_edge']:.2f}% stitched={r['wfa_stitched']:.1f}%",
            flush=True,
        )
        for y, ys in sorted(r["year_stats"].items()):
            print(
                f"  {y}: ret={ys['return_pct']:.1f}% maxDD={ys['max_dd_pct']:.1f}% "
                f"trades={ys['trades']} sells={ys['sells']}",
                flush=True,
            )

    print("\n## 极端年调仓日候选池", flush=True)
    print("| 方案 | 日期 | 合格池 | pool_min | 达标 | 放宽级别 | 排雷扣分 | 增强扣分 |", flush=True)
    print("|---|---|---:|---:|:---:|---|---:|---:|", flush=True)
    for name, rows in screening.items():
        for row in rows:
            ok = "Y" if row.get("pool_sufficient") else "N"
            print(
                f"| {name} | {row['date']} | {row['pool_count']} | {row['pool_min']} | {ok} | "
                f"{row.get('relaxation')} | {row.get('risk_penalized')} | {row.get('enhanced_penalized')} |",
                flush=True,
            )

    if len(results) == 2:
        b, a = results
        print("\n## 优化前后差值(后-前)", flush=True)
        print(f"- 总收益: {(a['total_return_pct'] or 0) - (b['total_return_pct'] or 0):+.1f}%")
        print(f"- 最大回撤: {(a['max_drawdown_pct'] or 0) - (b['max_drawdown_pct'] or 0):+.1f}%")
        print(f"- WFA 平均超额: {(a['wfa_mean_edge'] or 0) - (b['wfa_mean_edge'] or 0):+.2f}%")
        for y in EXTREME_YEARS:
            if y in a["year_stats"] and y in b["year_stats"]:
                dr = (a["year_stats"][y]["return_pct"] or 0) - (b["year_stats"][y]["return_pct"] or 0)
                dd = (a["year_stats"][y]["max_dd_pct"] or 0) - (b["year_stats"][y]["max_dd_pct"] or 0)
                print(f"- {y} 年收益: {dr:+.1f}%  年内回撤: {dd:+.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
