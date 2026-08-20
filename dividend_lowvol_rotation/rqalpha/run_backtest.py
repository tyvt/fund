# -*- coding: utf-8 -*-
"""RQAlpha 回测入口（Python API，不依赖 rqalpha CLI）。"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dividend_lowvol_rotation.config import (  # noqa: E402
    BACKTEST_INITIAL_CAPITAL,
    BACKTEST_OUTPUT_DIR,
    BACKTEST_REBALANCE_MODE,
    BACKTEST_YEARS,
    MIN_COMMISSION_CNY,
    RQALPHA_ADJUST_TYPE,
    RQALPHA_BUNDLE_PATH,
    TOP_N_BUY,
    uses_rqalpha_price_source,
)
from dividend_lowvol_rotation.rqalpha.execution_costs import (  # noqa: E402
    execution_cost_summary,
    rqalpha_commission_multiplier,
    rqalpha_engine_slippage_rate,
)
from dividend_lowvol_rotation.rqalpha.generate_report import generate_backtest_report  # noqa: E402
from market_data import configure_stdout_utf8  # noqa: E402


def _default_start(years: int, *, end: str | None = None) -> str:
    anchor = date.fromisoformat(end) if end else date.today()
    return (anchor - timedelta(days=int(365.25 * years))).isoformat()


def build_config(
    *,
    start: str,
    end: str,
    capital: float,
    strategy_file: Path,
    output_file: Path,
    plot: bool,
) -> dict:
    # 佣金：RQ 引擎扣费；成交价默认收盘价（无滑点）
    commission_multiplier = rqalpha_commission_multiplier()
    engine_slippage = rqalpha_engine_slippage_rate()
    return {
        "base": {
            "start_date": start,
            "end_date": end,
            "frequency": "1d",
            "accounts": {"stock": capital},
            "benchmark": "000300.XSHG",
            "data_bundle_path": RQALPHA_BUNDLE_PATH,
        },
        "extra": {
            "log_level": "info",
        },
        "mod": {
            "sys_analyser": {
                "enabled": True,
                "plot": plot,
                "output_file": str(output_file),
                "benchmark": "000300.XSHG",
            },
            "sys_progress": {
                "enabled": True,
                "show": True,
            },
            "sys_transaction_cost": {
                "enabled": True,
                "stock_commission_multiplier": commission_multiplier,
                "stock_min_commission": MIN_COMMISSION_CNY,
            },
            "sys_simulation": {
                "enabled": True,
                "slippage_model": "PriceRatioSlippage",
                "slippage": engine_slippage,
            },
            "sys_accounts": {
                "enabled": True,
                # 派息日预扣税由策略 dividend_tax_sync 处理；关闭卖出时补扣避免重复
                "dividend_tax_enabled": False,
                "stock_t1": True,
            },
        },
        "strategy_file": str(strategy_file),
    }


def main(argv: list[str] | None = None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="红利低波 RQAlpha 回测")
    parser.add_argument("--start", default=None, help="回测起点（默认近 N 年）")
    parser.add_argument("--end", default=None, help="回测终点（默认今天）")
    parser.add_argument("--years", type=int, default=BACKTEST_YEARS, help="未指定 start 时的年数")
    parser.add_argument("--capital", type=float, default=BACKTEST_INITIAL_CAPITAL)
    parser.add_argument("--top", type=int, default=TOP_N_BUY)
    parser.add_argument(
        "--rebalance-mode",
        default=BACKTEST_REBALANCE_MODE,
        choices=["monthly", "index_annual", "entry_anniversary", "quarterly_report", "fixed_days"],
    )
    parser.add_argument("--plot", action="store_true", help="回测结束后绘图")
    parser.add_argument(
        "--output",
        default=None,
        help="sys_analyser 输出 pkl 路径（默认 output/dividend_lowvol/rqalpha_result.pkl）",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="报告输出目录（默认 output/dividend_lowvol）",
    )
    parser.add_argument(
        "--report-basename",
        default="rqalpha_backtest",
        help="报告文件名前缀（默认 rqalpha_backtest → rqalpha_backtest.md/html）",
    )
    parser.add_argument("--no-report", action="store_true", help="跳过 Markdown/HTML 报告生成")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="仅生成报告（跳过 RQAlpha 引擎，复用原生 run_backtest）",
    )
    args = parser.parse_args(argv)

    start = args.start or _default_start(args.years, end=args.end or date.today().isoformat())
    end = args.end or date.today().isoformat()
    strategy_file = Path(__file__).resolve().parent / "dividend_lowvol_strategy.py"
    out_dir = Path(args.output_dir) if args.output_dir else BACKTEST_OUTPUT_DIR
    output_file = Path(args.output) if args.output else out_dir / "rqalpha_result.pkl"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if args.report_only:
        print(f"仅生成报告：{start} ~ {end}，初始资金 {args.capital:,.0f}")
        paths, _ = generate_backtest_report(
            start=start,
            end=end,
            top_n=args.top,
            rebalance_mode=args.rebalance_mode,
            initial_capital=args.capital,
            out_dir=out_dir,
            report_basename=args.report_basename,
        )
        print("\n已写入：")
        for k, p in paths.items():
            if p.exists():
                print(f"  {k}: {p}")
        return 0

    os.environ["DLV_RQALPHA_START"] = start
    os.environ["DLV_RQALPHA_END"] = end
    os.environ["DLV_RQALPHA_TOP_N"] = str(args.top)
    os.environ["DLV_RQALPHA_REBALANCE_MODE"] = args.rebalance_mode
    os.environ["DLV_RQALPHA_CAPITAL"] = str(args.capital)

    try:
        from rqalpha import run_file
    except ImportError:
        print(
            "未安装 RQAlpha。请先运行 scripts/setup_rqalpha_env.bat，"
            "或：pip install rqalpha && rqalpha download-bundle"
        )
        return 1

    config = build_config(
        start=start,
        end=end,
        capital=args.capital,
        strategy_file=strategy_file,
        output_file=output_file,
        plot=args.plot,
    )
    print(f"RQAlpha 回测：{start} ~ {end}，初始资金 {args.capital:,.0f}")
    print(f"交易成本：{execution_cost_summary()}")
    print(f"行情源：RQAlpha bundle（adjust={RQALPHA_ADJUST_TYPE}，path={RQALPHA_BUNDLE_PATH}）")
    if not uses_rqalpha_price_source():
        print("提示：原生回测未设 DLV_BACKTEST_PRICE_SOURCE=rqalpha，对比可能仍不一致")
    print(f"策略文件：{strategy_file}")
    print(f"结果输出：{output_file}")

    try:
        result = run_file(str(strategy_file), config)
    except TypeError:
        result = run_file(strategy_file=strategy_file, config=config)
    if result is None:
        print("回测未返回结果，请检查 RQAlpha 数据包是否已下载（rqalpha download-bundle）")
        return 2

    summary = result.get("summary") if isinstance(result, dict) else None
    if summary:
        print("\n--- RQAlpha 摘要 ---")
        for key in (
            "total_returns",
            "annualized_returns",
            "max_drawdown",
            "sharpe",
            "turnover",
        ):
            if key in summary:
                print(f"  {key}: {summary[key]}")
    print(f"\n完整结果已写入 {output_file}")

    if not args.no_report:
        print("\n--- 生成回测报告（复用原生 backtest + backtest_report）---")
        paths, _ = generate_backtest_report(
            start=start,
            end=end,
            top_n=args.top,
            rebalance_mode=args.rebalance_mode,
            initial_capital=args.capital,
            out_dir=out_dir,
            report_basename=args.report_basename,
            print_report=False,
        )
        print("已写入：")
        for k, p in paths.items():
            if p.exists():
                print(f"  {k}: {p}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
