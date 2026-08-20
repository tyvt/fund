# -*- coding: utf-8 -*-
"""对比 RQAlpha 回测与原生 backtest.py 基准线。"""

from __future__ import annotations

import argparse
import pickle
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd

from dividend_lowvol_rotation.backtest import run_backtest
from dividend_lowvol_rotation.config import BACKTEST_OUTPUT_DIR, BACKTEST_YEARS, TOP_N_BUY, uses_rqalpha_price_source
from market_data import configure_stdout_utf8


def _default_start(years: int, *, end: str | None = None) -> str:
    anchor = date.fromisoformat(end) if end else date.today()
    return (anchor - timedelta(days=int(365.25 * years))).isoformat()


def _load_rqalpha_native_nav(path: Path) -> pd.Series | None:
    """策略内按原生口径记录的净值（台账现金 + store 价）。"""
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        first = f.readline().strip()
    if first.startswith("date,"):
        df = pd.read_csv(path)
    elif first and first[0].isdigit():
        # 首行非表头（并发写入残留）时跳过
        df = pd.read_csv(path, header=1)
    else:
        df = pd.read_csv(path)
    if df.empty or "nav" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values("date").groupby("date", as_index=False).last()
    return df.set_index("date")["nav"].astype(float)


def _load_rqalpha_nav(pkl_path: Path) -> pd.DataFrame | None:
    if not pkl_path.exists():
        return None
    with pkl_path.open("rb") as f:
        data = pickle.load(f)
    if not isinstance(data, dict):
        return None
    portfolio = data.get("portfolio")
    if not isinstance(portfolio, pd.DataFrame) or "unit_net_value" not in portfolio.columns:
        return None
    start_cash = 1_000_000.0
    summary = data.get("summary")
    if isinstance(summary, dict):
        start_cash = float(summary.get("STOCK", start_cash))
    return pd.DataFrame(
        {
            "date": pd.to_datetime(portfolio.index).strftime("%Y-%m-%d"),
            "nav": portfolio["unit_net_value"].astype(float).values * start_cash,
        }
    )


def _load_rqalpha_summary(pkl_path: Path) -> dict:
    if not pkl_path.exists():
        return {}
    with pkl_path.open("rb") as f:
        data = pickle.load(f)
    summary = data.get("summary") if isinstance(data, dict) else None
    if not isinstance(summary, dict):
        return {}
    out = {}
    if "total_returns" in summary:
        out["total_return_pct"] = round(float(summary["total_returns"]) * 100, 2)
    if "annualized_returns" in summary:
        out["cagr_pct"] = round(float(summary["annualized_returns"]) * 100, 2)
    if "max_drawdown" in summary:
        out["max_drawdown_pct"] = round(float(summary["max_drawdown"]) * -100, 2)
    if "sharpe" in summary:
        out["sharpe"] = round(float(summary["sharpe"]), 3)
    if "turnover" in summary:
        out["turnover"] = round(float(summary["turnover"]), 3)
    if "total_value" in summary:
        out["final_nav"] = round(float(summary["total_value"]), 2)
    return out


def _metrics(nav: pd.Series) -> dict:
    if nav.empty or len(nav) < 2:
        return {}
    total_ret = nav.iloc[-1] / nav.iloc[0] - 1
    t0 = pd.Timestamp(nav.index[0])
    t1 = pd.Timestamp(nav.index[-1])
    years = max((t1 - t0).days / 365.25, 1 / 365)
    cagr = (1 + total_ret) ** (1 / years) - 1
    rets = nav.pct_change().dropna()
    sharpe = None
    if len(rets) > 2 and rets.std() > 0:
        sharpe = float(rets.mean() / rets.std() * (252**0.5))
    dd = (nav / nav.cummax() - 1).min()
    return {
        "total_return_pct": round(total_ret * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(dd * 100, 2),
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "nav_start": t0.strftime("%Y-%m-%d"),
        "nav_end": t1.strftime("%Y-%m-%d"),
        "nav_days": int(len(nav)),
    }


def _align_nav_for_metrics(native_nav: pd.Series, rq_nav: pd.Series) -> tuple[pd.Series, pd.Series, list[str]]:
    """指标在共有交易日上计算。"""
    notes: list[str] = []
    if native_nav.empty or rq_nav.empty:
        return native_nav, rq_nav, notes
    common = native_nav.index.intersection(rq_nav.index)
    if len(common) < 2:
        return native_nav, rq_nav, notes
    common = sorted(common)
    nat_aligned = native_nav.reindex(common)
    rq_aligned = rq_nav.reindex(common)
    notes.append(
        f"指标对齐区间：{common[0]} ~ {common[-1]}（共有 {len(common)} 个交易日）"
    )
    return nat_aligned, rq_aligned, notes


def _rebalance_trade_diff(trades: pd.DataFrame, rq_trades: pd.DataFrame) -> list[str]:
    """调仓日成交差异（按 signed delta）。"""
    lines: list[str] = []
    if trades.empty or rq_trades.empty:
        return lines

    nat = trades.copy()
    nat["side_en"] = nat["side"].map({"买入": "buy", "卖出": "sell"})
    rq = rq_trades.copy()
    rq["code"] = rq["order_book_id"].str.split(".").str[0]
    rq["side_en"] = rq["side"].map({"BUY": "buy", "SELL": "sell", 1: "buy", 2: "sell"})

    rb_dates = sorted(set(nat["date"].unique()) | set(rq["date"].unique()))
    big = [d for d in rb_dates if len(nat[nat.date == d]) >= 3 or len(rq[rq.date == d]) >= 3]
    for d in big:
        def _signed(df):
            out: dict[str, int] = {}
            for _, r in df.iterrows():
                s = 1 if r["side_en"] == "buy" else -1
                qty = int(r.get("shares", r.get("last_quantity", 0)))
                out[r["code"]] = out.get(r["code"], 0) + s * qty
            return out

        n_signed = _signed(nat[nat["date"] == d])
        r_signed = _signed(rq[rq["date"] == d])
        diffs = [
            (c, n_signed.get(c, 0), r_signed.get(c, 0))
            for c in sorted(set(n_signed) | set(r_signed))
            if n_signed.get(c, 0) != r_signed.get(c, 0)
        ]
        lines.append(f"### {d}（差异 {len(diffs)} 项）")
        if not diffs:
            lines.append("- 成交一致")
        else:
            for c, a, b in diffs[:12]:
                lines.append(f"- {c}: 原生 {a:+d} / RQ {b:+d}")
            if len(diffs) > 12:
                lines.append(f"- … 另有 {len(diffs) - 12} 项")
        lines.append("")
    return lines


def main(argv: list[str] | None = None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="RQAlpha vs 原生回测对比")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--years", type=int, default=BACKTEST_YEARS)
    parser.add_argument("--top", type=int, default=TOP_N_BUY)
    parser.add_argument(
        "--rqalpha-pkl",
        default=str(BACKTEST_OUTPUT_DIR / "rqalpha_result.pkl"),
        help="RQAlpha sys_analyser 输出文件",
    )
    parser.add_argument("--skip-native", action="store_true", help="仅解析 RQAlpha 结果")
    args = parser.parse_args(argv)

    start = args.start or _default_start(args.years, end=args.end or date.today().isoformat())
    end = args.end or date.today().isoformat()
    pkl_path = Path(args.rqalpha_pkl)

    # 与 RQAlpha 回测本金对齐（pkl summary 或默认 100 万）
    initial_capital = 1_000_000.0
    if pkl_path.exists():
        with pkl_path.open("rb") as f:
            pkl_data = pickle.load(f)
        summary = pkl_data.get("summary") if isinstance(pkl_data, dict) else None
        if isinstance(summary, dict) and "STOCK" in summary:
            initial_capital = float(summary["STOCK"])

    print(f"对比区间：{start} ~ {end}\n")

    trades_df = pd.DataFrame()
    nav_df = pd.DataFrame()
    native_nav = pd.Series(dtype=float)
    native_m: dict = {}
    rq_m: dict = {}
    meta: dict = {}
    metric_notes: list[str] = []
    if not args.skip_native:
        print("运行原生回测（基准线）…")
        nav_df, trades_df, holdings_df, _, meta, _ = run_backtest(
            start=start,
            end=end,
            top_n=args.top,
            initial_capital=initial_capital,
            verbose=False,
        )
        if nav_df.empty:
            print("原生回测无净值数据")

    rq_nav_df = _load_rqalpha_native_nav(BACKTEST_OUTPUT_DIR / "rqalpha_native_nav.csv")
    rq_engine_nav_df = _load_rqalpha_nav(pkl_path)
    if rq_nav_df is None and rq_engine_nav_df is not None:
        rq_nav_df = rq_engine_nav_df

    rq_nav = pd.Series(dtype=float)
    if rq_nav_df is not None and not (hasattr(rq_nav_df, "empty") and rq_nav_df.empty):
        rq_nav = (
            rq_nav_df
            if isinstance(rq_nav_df, pd.Series)
            else rq_nav_df.set_index("date")["nav"].astype(float)
        )

    if not nav_df.empty:
        nav_dedup = nav_df.copy()
        nav_dedup["date"] = pd.to_datetime(nav_dedup["date"])
        nav_dedup = nav_dedup.sort_values("date").groupby("date", as_index=False).last()
        native_nav = nav_dedup.set_index(nav_dedup["date"].dt.strftime("%Y-%m-%d"))["nav"].astype(float)

    if not native_nav.empty and not rq_nav.empty:
        native_for_m, rq_for_m, metric_notes = _align_nav_for_metrics(native_nav, rq_nav)
        native_m = _metrics(native_for_m)
        rq_m = {**_load_rqalpha_summary(pkl_path), **_metrics(rq_for_m), "nav_source": "native_ledger"}
        rq_m["final_nav"] = round(float(rq_for_m.iloc[-1]), 2)
    elif not native_nav.empty:
        native_m = _metrics(native_nav)
        rq_m = _load_rqalpha_summary(pkl_path)
    elif not rq_nav.empty:
        rq_m = {**_load_rqalpha_summary(pkl_path), **_metrics(rq_nav), "nav_source": "native_ledger"}
        rq_m["final_nav"] = round(float(rq_nav.iloc[-1]), 2)
    else:
        rq_m = _load_rqalpha_summary(pkl_path)

    if native_m:
        print("【原生 backtest.py】")
        if metric_notes:
            for note in metric_notes:
                print(f"  ({note})")
        for k, v in native_m.items():
            if k.startswith("nav_"):
                continue
            print(f"  {k}: {v}")
        if meta:
            print(f"  rebalance_count: {meta.get('rebalance_count')}")
    if not rq_m:
        print(f"\n未找到或未解析 RQAlpha 结果：{pkl_path}")
        print("请先运行：python -m dividend_lowvol_rotation.rqalpha.run_backtest")
        return 1

    print("\n【RQAlpha】")
    if rq_m.get("nav_source") == "native_ledger":
        print("  (净值口径：原生台账 + store 收盘价，与 backtest.py 一致)")
    for k, v in rq_m.items():
        if k in ("nav_source",) or k.startswith("nav_"):
            continue
        print(f"  {k}: {v}")

    if native_m and rq_m:
        print("\n【差异 (RQAlpha - 原生)】")
        for key in ("total_return_pct", "cagr_pct", "max_drawdown_pct"):
            if key in native_m and key in rq_m:
                delta = rq_m[key] - native_m[key]
                print(f"  {key}: {delta:+.2f} pp")
        if "final_nav" in rq_m and native_m.get("nav_end"):
            nat_final = float(native_nav.reindex([native_m["nav_end"]]).iloc[0])
            rq_final = float(rq_m["final_nav"])
            print(f"  final_nav: {rq_final - nat_final:+,.2f} 元")
        if not native_nav.empty and not rq_nav.empty:
            common = native_nav.index.intersection(rq_nav.index)
            if len(common) > 0:
                gap = (rq_nav.reindex(common).round(2) - native_nav.reindex(common).round(2)).abs()
                max_gap = float(gap.max()) if not gap.empty else 0.0
                print(f"  max_daily_nav_gap: {max_gap:,.2f} 元")

    out_md = BACKTEST_OUTPUT_DIR / "rqalpha_vs_native.md"
    out_md.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# RQAlpha vs 原生回测对比",
        "",
        f"- 区间：{start} ~ {end}",
        f"- Top N：{args.top}",
        f"- 初始本金：{initial_capital:,.0f}",
        f"- 行情源：{'RQAlpha bundle（两边同源）' if uses_rqalpha_price_source() else '原生 DuckDB（未统一，见 DLV_BACKTEST_PRICE_SOURCE）'}",
    ]
    for note in metric_notes:
        lines.append(f"- {note}")
    lines.extend(["", "## 原生 backtest.py", ""])
    for k, v in native_m.items():
        if k.startswith("nav_"):
            continue
        lines.append(f"- {k}: {v}")
    lines.extend(["", "## RQAlpha", ""])
    if rq_m.get("nav_source") == "native_ledger":
        lines.append("- nav_source: 原生台账 + store 收盘价")
    for k, v in rq_m.items():
        if k in ("nav_source",) or k.startswith("nav_"):
            continue
        lines.append(f"- {k}: {v}")

    if native_m and rq_m:
        lines.extend(["", "## 差异 (RQAlpha - 原生)", ""])
        for key in ("total_return_pct", "cagr_pct", "max_drawdown_pct", "sharpe"):
            if key in native_m and key in rq_m:
                delta = rq_m[key] - native_m[key]
                lines.append(f"- {key}: {delta:+.2f} pp")
        if "final_nav" in rq_m and native_m.get("nav_end"):
            nat_final = float(native_nav.reindex([native_m["nav_end"]]).iloc[0])
            rq_final = float(rq_m["final_nav"])
            lines.append(f"- final_nav: {rq_final - nat_final:+,.2f} 元")
        if not native_nav.empty and not rq_nav.empty:
            common = native_nav.index.intersection(rq_nav.index)
            if len(common) > 0:
                gap = (rq_nav.reindex(common).round(2) - native_nav.reindex(common).round(2)).abs()
                max_gap = float(gap.max()) if not gap.empty else 0.0
                lines.append(f"- max_daily_nav_gap: {max_gap:,.2f} 元")

    trade_lines: list[str] = []
    if not trades_df.empty and pkl_path.exists():
        with pkl_path.open("rb") as f:
            pkl_data = pickle.load(f)
        rq_tr = pkl_data.get("trades") if isinstance(pkl_data, dict) else None
        if isinstance(rq_tr, pd.DataFrame) and not rq_tr.empty:
            rq_tr = rq_tr.copy()
            rq_tr["date"] = pd.to_datetime(rq_tr["datetime"]).dt.strftime("%Y-%m-%d")
            trade_lines = _rebalance_trade_diff(trades_df, rq_tr)
    if trade_lines:
        lines.extend(["", "## 调仓日成交差异", ""] + trade_lines)

    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n对比报告：{out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
