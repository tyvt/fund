#!/usr/bin/env python
"""Validate the full-rule VectorBT replay against a frozen RQAlpha baseline."""

from __future__ import annotations

import argparse
import json
import os
import pickle
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def export_baseline(source: Path, target: Path) -> int:
    """Run under rqalpha_env and export only portable scalar/tabular fields."""
    target.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as handle:
        payload = pickle.load(handle)
    summary = {key: value for key, value in payload["summary"].items() if isinstance(value, (str, int, float, bool, type(None)))}
    (target / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    portfolio = payload["portfolio"][["total_value"]].reset_index()
    portfolio.to_csv(target / "portfolio.csv", index=False, encoding="utf-8-sig")
    positions = payload["stock_positions"][["order_book_id", "quantity", "last_price", "market_value"]].reset_index()
    positions.to_csv(target / "positions.csv", index=False, encoding="utf-8-sig")
    account = payload["stock_account"][["cash", "transaction_cost", "market_value", "total_value"]].reset_index()
    account.to_csv(target / "stock_account.csv", index=False, encoding="utf-8-sig")
    trades = payload["trades"][["datetime", "order_book_id", "side", "last_quantity", "last_price", "commission", "tax"]].copy()
    trades["side"] = trades["side"].astype(str)
    trades.to_csv(target / "trades.csv", index=False, encoding="utf-8-sig")
    return 0


def ensure_portable_baseline(source: Path) -> Path:
    cache = ROOT / "cache/vectorbt/baseline_exports" / source.stem
    expected = (cache / "summary.json", cache / "portfolio.csv", cache / "positions.csv", cache / "stock_account.csv", cache / "trades.csv")
    if all(path.exists() and path.stat().st_mtime >= source.stat().st_mtime for path in expected):
        return cache
    rqpython = ROOT / "rqalpha_env/Scripts/python.exe"
    if not rqpython.exists():
        raise FileNotFoundError("找不到 rqalpha_env/Scripts/python.exe，无法读取 RQAlpha 基准")
    environment = os.environ.copy()
    environment["MPLBACKEND"] = "Agg"
    subprocess.run(
        [str(rqpython), str(Path(__file__).resolve()), "--export-baseline", str(source), "--export-dir", str(cache)],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    return cache


def latest_codes(frame: pd.DataFrame) -> set[str]:
    if frame.empty:
        return set()
    date = pd.to_datetime(frame["date"]).max()
    current = frame[pd.to_datetime(frame["date"]).eq(date)]
    return {str(code).split(".")[0].zfill(6) for code in current.loc[current["quantity"].gt(0), "order_book_id"]}


def run_validation(args) -> int:
    from vbt.adapters import VBTDataLoader
    from vbt.config import load_backtest_config, load_strategy_config
    from vbt.engine import PerformanceCalculator, VBTEngine
    from vbt.engine.reporter import _markdown_table
    from vbt.strategies import DividendLowVolStrategy

    baseline_path = Path(args.baseline)
    if not baseline_path.is_absolute():
        baseline_path = ROOT / baseline_path
    portable = ensure_portable_baseline(baseline_path)
    summary = json.loads((portable / "summary.json").read_text(encoding="utf-8"))
    rq_nav = pd.read_csv(portable / "portfolio.csv", parse_dates=["date"]).set_index("date")["total_value"].astype(float)
    rq_positions = pd.read_csv(portable / "positions.csv", dtype={"order_book_id": str}, parse_dates=["date"])
    start = args.start or str(summary["start_date"])
    end = args.end or str(summary["end_date"])
    config = load_backtest_config({"start_date": start, "end_date": end, "initial_capital": args.capital})
    params = load_strategy_config()
    started = time.perf_counter()
    data = VBTDataLoader(start_date=start, end_date=end, cache_enabled=True, cache_dir=config["cache_dir"]).load_baseline_aligned(
        baseline_path, initial_capital=args.capital
    )
    engine = VBTEngine(
        data=data,
        strategy=DividendLowVolStrategy(params),
        initial_capital=args.capital,
        commission=config["commission"],
        min_commission=config["min_commission"],
        stamp_duty_before=config["stamp_duty_before_2023_08_28"],
        stamp_duty_after=config["stamp_duty_after_2023_08_28"],
        slippage=config["slippage"],
        backtest_config=config,
    )
    result = engine.run()
    elapsed = time.perf_counter() - started
    vbt_metrics = PerformanceCalculator(result).compute_metrics()
    rq_annual = float(summary["annualized_returns"])
    rq_drawdown = -abs(float(summary["max_drawdown"]))
    annual_diff = abs(vbt_metrics["annual_return"] - rq_annual)
    drawdown_diff = abs(vbt_metrics["max_drawdown"] - rq_drawdown)
    vbt_codes = set(result.shares.columns[result.shares.iloc[-1].gt(0)])
    rq_codes = latest_codes(rq_positions)
    denominator = max(1, min(10, len(rq_codes)))
    overlap = len(vbt_codes & rq_codes) / denominator
    aligned = pd.concat([result.nav.rename("vectorbt"), rq_nav.rename("rqalpha")], axis=1, join="inner").dropna()
    nav_mae = float((aligned["vectorbt"] - aligned["rqalpha"]).abs().mean()) if not aligned.empty else float("nan")
    rows = pd.DataFrame(
        [
            {"验收项": "年化收益差", "VectorBT": f"{vbt_metrics['annual_return']:.4%}", "RQAlpha": f"{rq_annual:.4%}", "差异": f"{annual_diff:.4%}", "阈值": "< 0.5%", "结果": "通过" if annual_diff < 0.005 else "失败"},
            {"验收项": "最大回撤差", "VectorBT": f"{vbt_metrics['max_drawdown']:.4%}", "RQAlpha": f"{rq_drawdown:.4%}", "差异": f"{drawdown_diff:.4%}", "阈值": "< 1%", "结果": "通过" if drawdown_diff < 0.01 else "失败"},
            {"验收项": "期末持仓 Top10 重合率", "VectorBT": ", ".join(sorted(vbt_codes)), "RQAlpha": ", ".join(sorted(rq_codes)), "差异": f"{overlap:.2%}", "阈值": "> 80%", "结果": "通过" if overlap > 0.80 else "失败"},
        ]
    )
    passed = bool(rows["结果"].eq("通过").all())
    output = ROOT / "output/vectorbt/reports/validation_report.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    report = "\n".join(
        [
            "# VectorBT 与 RQAlpha 对齐验收",
            "",
            f"- 区间：{start} ～ {end}",
            f"- 初始资金：{args.capital:,.2f}",
            f"- VectorBT 耗时：{elapsed:.2f} 秒",
            f"- 共同净值交易日：{len(aligned)}",
            f"- 逐日净值平均绝对差：{nav_mae:,.4f}",
            f"- 总结：**{'全部通过' if passed else '存在失败项'}**",
            "",
            _markdown_table(rows),
            "",
        ]
    )
    output.write_text(report, encoding="utf-8")
    print(report)
    print(f"验收报告：{output}")
    return 0 if passed else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VectorBT / RQAlpha 对齐验收")
    parser.add_argument("--years", type=int, default=10, help="保留用于兼容验收命令；区间以基准摘要为准")
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--baseline", default="output/vectorbt/baselines/rqalpha_parquet_10y_20160819_20260819.pkl")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--export-baseline", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--export-dir", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.export_baseline:
        raise SystemExit(export_baseline(arguments.export_baseline, arguments.export_dir))
    raise SystemExit(run_validation(arguments))
