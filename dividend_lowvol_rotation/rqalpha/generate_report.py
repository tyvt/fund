# -*- coding: utf-8 -*-
"""RQAlpha 回测结束后生成与原生 backtest.py 一致的 Markdown / HTML 报告。"""

from __future__ import annotations

import time
from pathlib import Path

from dividend_lowvol_rotation.backtest import run_backtest
from dividend_lowvol_rotation.backtest_report import format_backtest_report, save_backtest_outputs
from dividend_lowvol_rotation.config import BACKTEST_OUTPUT_DIR, BACKTEST_REBALANCE_MODE, TOP_N_BUY


def generate_backtest_report(
    *,
    start: str,
    end: str,
    top_n: int = TOP_N_BUY,
    rebalance_mode: str = BACKTEST_REBALANCE_MODE,
    initial_capital: float,
    out_dir: Path | None = None,
    report_basename: str = "rqalpha_backtest",
    print_report: bool = True,
    verbose_backtest: bool = False,
) -> tuple[dict[str, Path], dict]:
    """调用原生 run_backtest 组装数据，复用 backtest_report 写 md/html。

    RQAlpha 与原生引擎已对齐，报告数据来自原生回测（与 RQ 逐日 NAV 一致）。
    """
    out_dir = out_dir or BACKTEST_OUTPUT_DIR
    html_name = f"{report_basename}.html"
    t0 = time.time()

    nav_df, trades_df, holdings_df, summary_df, meta, dividend_tax_df = run_backtest(
        start=start,
        end=end,
        top_n=top_n,
        rebalance_mode=rebalance_mode,
        initial_capital=initial_capital,
        verbose=verbose_backtest,
    )
    meta = dict(meta)
    meta["html_report_name"] = html_name
    meta["report_basename"] = report_basename
    meta["report_source"] = "native_engine_aligned_with_rqalpha"

    report = format_backtest_report(nav_df, trades_df, summary_df, meta, dividend_tax_df)
    if print_report:
        print(report)
        print(f"\n报告生成耗时：**{time.time() - t0:.0f}** 秒")

    paths = save_backtest_outputs(
        out_dir,
        nav_df,
        trades_df,
        holdings_df,
        summary_df,
        meta,
        dividend_tax_df,
        report_basename=report_basename,
    )
    return paths, meta
