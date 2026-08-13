# -*- coding: utf-8 -*-
"""增强因子消融：逐一关闭因子，对比 10 年回测绩效。"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# 需在首次 import config 前可覆盖的环境变量键
ENHANCED_ENV_KEYS = (
    "DLV_SUSTAINABLE_DIVIDEND_ENABLED",
    "DLV_YIELD_SPREAD_PERCENTILE_ENABLED",
    "DLV_PROFIT_MOMENTUM_FILTER_ENABLED",
    "DLV_PROFIT_STABILITY_FILTER_ENABLED",
    "DLV_DIVIDEND_COVERAGE_FILTER_ENABLED",
    "DLV_BETA_BALANCE_ENABLED",
    "DLV_INDEX_ANNUAL_REBALANCE_TIMING",
)

RELOAD_MODULES = (
    "dividend_lowvol_rotation.config",
    "dividend_lowvol_rotation.enhanced_factors",
    "dividend_lowvol_rotation.index_portfolio",
    "dividend_lowvol_rotation.industry_caps",
    "dividend_lowvol_rotation.rebalance_schedule",
    "dividend_lowvol_rotation.scoring",
    "dividend_lowvol_rotation.backtest",
)

SCENARIOS: dict[str, dict[str, str]] = {
    "current": {
        "label": "当前默认（全部增强因子 + 1月调仓）",
    },
    "legacy_december": {
        "label": "关闭全部增强因子 + 12月调仓（对标旧版）",
        "DLV_SUSTAINABLE_DIVIDEND_ENABLED": "false",
        "DLV_YIELD_SPREAD_PERCENTILE_ENABLED": "false",
        "DLV_PROFIT_MOMENTUM_FILTER_ENABLED": "false",
        "DLV_PROFIT_STABILITY_FILTER_ENABLED": "false",
        "DLV_DIVIDEND_COVERAGE_FILTER_ENABLED": "false",
        "DLV_BETA_BALANCE_ENABLED": "false",
        "DLV_INDEX_ANNUAL_REBALANCE_TIMING": "december",
    },
    "all_off_january": {
        "label": "关闭全部增强因子（保留1月调仓）",
        "DLV_SUSTAINABLE_DIVIDEND_ENABLED": "false",
        "DLV_YIELD_SPREAD_PERCENTILE_ENABLED": "false",
        "DLV_PROFIT_MOMENTUM_FILTER_ENABLED": "false",
        "DLV_PROFIT_STABILITY_FILTER_ENABLED": "false",
        "DLV_DIVIDEND_COVERAGE_FILTER_ENABLED": "false",
        "DLV_BETA_BALANCE_ENABLED": "false",
    },
    "w/o_sustainable_div": {
        "label": "关闭：可持续股息率排序",
        "DLV_SUSTAINABLE_DIVIDEND_ENABLED": "false",
    },
    "w/o_yield_spread": {
        "label": "关闭：利差分位陷阱",
        "DLV_YIELD_SPREAD_PERCENTILE_ENABLED": "false",
    },
    "w/o_profit_momentum": {
        "label": "关闭：盈利动量",
        "DLV_PROFIT_MOMENTUM_FILTER_ENABLED": "false",
    },
    "w/o_profit_stability": {
        "label": "关闭：盈利稳定性",
        "DLV_PROFIT_STABILITY_FILTER_ENABLED": "false",
    },
    "w/o_dividend_coverage": {
        "label": "关闭：分红覆盖率",
        "DLV_DIVIDEND_COVERAGE_FILTER_ENABLED": "false",
    },
    "w/o_beta_balance": {
        "label": "关闭：Beta分散",
        "DLV_BETA_BALANCE_ENABLED": "false",
    },
    "december_only": {
        "label": "仅改调仓：12月（增强因子保持）",
        "DLV_INDEX_ANNUAL_REBALANCE_TIMING": "december",
    },
}


def _reload_strategy_modules() -> None:
    import duckdb_cache

    duckdb_cache._DUCKDB_READY = None
    for name in RELOAD_MODULES:
        mod = importlib.import_module(name)
        importlib.reload(mod)


def _apply_scenario(overrides: dict[str, str]) -> None:
    for key in ENHANCED_ENV_KEYS:
        os.environ.pop(key, None)
    for key, val in overrides.items():
        if key.startswith("DLV_"):
            os.environ[key] = val
    _reload_strategy_modules()


def _run_one(
    start: str,
    end: str,
    *,
    verbose: bool = False,
) -> dict:
    from dividend_lowvol_rotation.backtest import prepare_backtest_context, run_backtest
    from dividend_lowvol_rotation.config import BACKTEST_REBALANCE_MODE

    t0 = time.perf_counter()
    ctx = prepare_backtest_context(start, end, verbose=verbose)
    _, trades_df, _, _, meta, _ = run_backtest(
        start=start,
        end=end,
        ctx=ctx,
        verbose=False,
        rebalance_mode=BACKTEST_REBALANCE_MODE,
    )
    elapsed = time.perf_counter() - t0
    trades = len(trades_df) if trades_df is not None and not trades_df.empty else 0
    return {
        "total_return_pct": meta.get("total_return_pct"),
        "cagr_pct": meta.get("cagr_pct"),
        "max_drawdown_pct": meta.get("max_drawdown_pct"),
        "sharpe": meta.get("sharpe"),
        "holdings": meta.get("holdings_count"),
        "trades": trades,
        "elapsed_sec": round(elapsed, 1),
    }


def _format_pct(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{float(v):+.2f}%"


def _write_report(rows: list[dict], path: Path, *, start: str, end: str) -> None:
    lines = [
        "# 增强因子消融测试",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"> 区间：{start} ~ {end}  ",
        f"> 持仓 10 只 · index_annual · index_rules",
        "",
        "| 场景 | 总收益 | 年化 | 最大回撤 | Sharpe | 成交笔数 | 耗时(s) |",
        "|------|--------|------|----------|--------|----------|---------|",
    ]
    baseline_ret = None
    for r in rows:
        if r["id"] == "current":
            baseline_ret = r.get("total_return_pct")
            break
    for r in rows:
        ret = r.get("total_return_pct")
        delta = ""
        if baseline_ret is not None and ret is not None:
            d = float(ret) - float(baseline_ret)
            delta = f" ({d:+.1f}pp)"
        sharpe = r.get("sharpe")
        sharpe_s = f"{float(sharpe):.2f}" if sharpe is not None else "—"
        lines.append(
            f"| {r['label']}{delta} | {_format_pct(ret)} | {_format_pct(r.get('cagr_pct'))} | "
            f"{_format_pct(r.get('max_drawdown_pct'))} | "
            f"{sharpe_s} | "
            f"{r.get('trades', '—')} | {r.get('elapsed_sec', '—')} |"
        )
    lines.extend(
        [
            "",
            "## 解读提示",
            "",
            "- **legacy_december**：最接近改造前规则（无增强因子 + 12月调仓）",
            "- **w/o_***：在当前默认基础上单独关闭一个因子，观察边际影响",
            "- 括号内为相对 `current` 的总收益差（百分点）",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    from dividend_lowvol_rotation.backtest import default_start_years
    from dividend_lowvol_rotation.config import BACKTEST_OUTPUT_DIR
    from market_data import configure_stdout_utf8

    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="增强因子消融回测")
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--end", default="2025-08-01")
    parser.add_argument("--start", default=None)
    parser.add_argument(
        "--scenarios",
        nargs="*",
        default=None,
        help="指定场景 id，默认全部",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    start = args.start or default_start_years(args.years)
    end = args.end
    ids = args.scenarios or list(SCENARIOS.keys())
    out_path = BACKTEST_OUTPUT_DIR / "factor_ablation.md"

    rows: list[dict] = []
    total_t0 = time.perf_counter()
    for i, sid in enumerate(ids, 1):
        if sid not in SCENARIOS:
            print(f"未知场景: {sid}", flush=True)
            continue
        spec = SCENARIOS[sid]
        label = spec.get("label", sid)
        overrides = {k: v for k, v in spec.items() if k != "label"}
        print(f"\n[{i}/{len(ids)}] {sid}: {label}", flush=True)
        _apply_scenario(overrides)
        try:
            metrics = _run_one(start, end, verbose=args.verbose)
            row = {"id": sid, "label": label, **metrics}
            rows.append(row)
            print(
                f"  → 总收益 {_format_pct(metrics.get('total_return_pct'))}  "
                f"年化 {_format_pct(metrics.get('cagr_pct'))}  "
                f"回撤 {_format_pct(metrics.get('max_drawdown_pct'))}  "
                f"成交 {metrics.get('trades')}  "
                f"({metrics.get('elapsed_sec')}s)",
                flush=True,
            )
        except Exception as exc:
            print(f"  → 失败: {exc}", flush=True)
            rows.append({"id": sid, "label": label, "error": str(exc)})

    _write_report(rows, out_path, start=start, end=end)
    print(f"\n报告已写入: {out_path}")
    print(f"总耗时: {time.perf_counter() - total_t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
