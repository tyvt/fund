# -*- coding: utf-8 -*-
"""降回撤参数扫描：共享 ctx，逐场景改 env 后完整回测。"""
from __future__ import annotations

import importlib
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

START = os.environ.get("DLV_SWEEP_START", "2016-08-13")
END = os.environ.get("DLV_SWEEP_END", "2025-08-01")

BASE_ENV = {
    "DLV_INDEX_ANNUAL_REBALANCE_TIMING": "january",
    "DLV_BACKTEST_KLINE_FQ": "qfq",
}

SCENARIOS: list[tuple[str, dict[str, str]]] = [
    ("当前默认(含日间风控)", {}),
    ("关闭日间风控", {"DLV_INDEX_RULES_DAILY_RISK_ENABLED": "false"}),
    ("市场降仓", {"DLV_MARKET_REGIME_ENABLED": "true"}),
    ("降仓+宽度", {"DLV_MARKET_REGIME_ENABLED": "true", "DLV_MARKET_BREADTH_ENABLED": "true"}),
    ("波动率目标16%", {"DLV_VOL_TARGET_ENABLED": "true", "DLV_VOL_TARGET_PCT": "16"}),
    ("三者全开", {
        "DLV_MARKET_REGIME_ENABLED": "true",
        "DLV_MARKET_BREADTH_ENABLED": "true",
        "DLV_VOL_TARGET_ENABLED": "true",
        "DLV_VOL_TARGET_PCT": "16",
    }),
    ("持仓5只", {"DLV_TOP_N_BUY": "5"}),
    ("持仓7只", {"DLV_TOP_N_BUY": "7"}),
    ("单股上限6%", {"DLV_MAX_SINGLE_STOCK_WEIGHT": "0.06"}),
    ("波动上限35%", {"DLV_MAX_ANNUALIZED_VOL_PCT": "35"}),
    ("高PE暂停买入", {
        "DLV_MARKET_VALUATION_PAUSE_BUYS_ENABLED": "true",
        "DLV_MARKET_VALUATION_PE_PAUSE_PCT": "85",
    }),
    ("降仓+5只+单股6%", {
        "DLV_MARKET_REGIME_ENABLED": "true",
        "DLV_TOP_N_BUY": "5",
        "DLV_MAX_SINGLE_STOCK_WEIGHT": "0.06",
    }),
    ("综合防守", {
        "DLV_MARKET_REGIME_ENABLED": "true",
        "DLV_MARKET_BREADTH_ENABLED": "true",
        "DLV_VOL_TARGET_ENABLED": "true",
        "DLV_VOL_TARGET_PCT": "16",
        "DLV_TOP_N_BUY": "7",
        "DLV_MAX_SINGLE_STOCK_WEIGHT": "0.06",
        "DLV_MAX_ANNUALIZED_VOL_PCT": "35",
        "DLV_MARKET_VALUATION_PAUSE_BUYS_ENABLED": "true",
        "DLV_MARKET_VALUATION_PE_PAUSE_PCT": "88",
    }),
]

RELOAD_MODULES = (
    "dividend_lowvol_rotation.config",
    "dividend_lowvol_rotation.scoring",
    "dividend_lowvol_rotation.risk_regime",
    "dividend_lowvol_rotation.dynamic_params",
    "dividend_lowvol_rotation.index_portfolio",
    "dividend_lowvol_rotation.backtest",
)


def reload_all() -> None:
    for name in RELOAD_MODULES:
        importlib.reload(importlib.import_module(name))


def clear_dlv_env() -> None:
    for k in list(os.environ):
        if k.startswith("DLV_") and k not in BASE_ENV:
            os.environ.pop(k, None)


def run_scenario(name: str, overrides: dict[str, str], ctx) -> dict:
    clear_dlv_env()
    for k, v in BASE_ENV.items():
        os.environ[k] = v
    for k, v in overrides.items():
        os.environ[k] = v
    reload_all()
    from dividend_lowvol_rotation.backtest import run_backtest

    t0 = time.perf_counter()
    _, trades, _, _, meta, _ = run_backtest(
        start=START,
        end=END,
        ctx=ctx,
        verbose=False,
        record_details=False,
    )
    sec = time.perf_counter() - t0
    return {
        "name": name,
        "total_return_pct": meta.get("total_return_pct"),
        "cagr_pct": meta.get("cagr_pct"),
        "max_drawdown_pct": meta.get("max_drawdown_pct"),
        "trades": meta.get("trade_count") or len(trades),
        "sec": sec,
    }


def main() -> int:
    for k, v in BASE_ENV.items():
        os.environ[k] = v
    reload_all()
    from dividend_lowvol_rotation.backtest import prepare_backtest_context

    print(f"预加载 ctx {START} ~ {END} …", flush=True)
    t0 = time.perf_counter()
    ctx = prepare_backtest_context(START, END, verbose=True)
    print(f"context ready in {time.perf_counter() - t0:.0f}s\n", flush=True)

    rows: list[dict] = []
    for name, overrides in SCENARIOS:
        print(f"运行: {name} …", flush=True)
        row = run_scenario(name, overrides, ctx)
        rows.append(row)
        print(
            f"  ret={row['total_return_pct']:.1f}% "
            f"CAGR={row['cagr_pct']:.1f}% "
            f"maxDD={row['max_drawdown_pct']:.1f}% "
            f"trades={row['trades']} "
            f"time={row['sec']:.0f}s",
            flush=True,
        )

    rows.sort(key=lambda r: r["max_drawdown_pct"] or 0, reverse=True)
    print("\n| 方案 | 总收益% | 年化% | 最大回撤% | 成交 | 耗时s |")
    print("|---|---:|---:|---:|---:|---:|")
    for r in rows:
        print(
            f"| {r['name']} | {r['total_return_pct']:.1f} | {r['cagr_pct']:.1f} | "
            f"{r['max_drawdown_pct']:.1f} | {r['trades']} | {r['sec']:.0f} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
