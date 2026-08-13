# -*- coding: utf-8 -*-
"""对比排雷因子开启前后的日频回测收益。"""
import os
import sys
import time
import importlib
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dividend_lowvol_rotation.backtest import default_start_years, prepare_backtest_context, run_backtest
from dividend_lowvol_rotation.config import TOP_N_BUY, resolve_sell_rank


def _reload_modules():
    import dividend_lowvol_rotation.config as cfg
    import dividend_lowvol_rotation.risk_screening as rs
    import dividend_lowvol_rotation.scoring as sc

    importlib.reload(cfg)
    importlib.reload(rs)
    importlib.reload(sc)
    return cfg


def run(label: str, env: dict[str, str], ctx, start: str, end: str, sell_rank: int) -> dict:
    for k, v in env.items():
        os.environ[k] = v
    _reload_modules()
    t0 = time.perf_counter()
    _, _, _, _, meta, _ = run_backtest(
        start=start,
        end=end,
        ctx=ctx,
        rebalance_days=1,
        sell_rank=sell_rank,
        record_details=False,
        verbose=False,
    )
    sec = time.perf_counter() - t0
    print(
        f"{label}: ret={meta.get('total_return_pct') or 0:.2f}% "
        f"CAGR={meta.get('cagr_pct') or 0:.2f}% "
        f"maxDD={meta.get('max_drawdown_pct') or 0:.2f}% "
        f"trades={meta.get('trade_count')} time={sec:.0f}s"
    )
    return meta


if __name__ == "__main__":
    years = int(os.environ.get("DLV_COMPARE_YEARS", "1"))
    end = os.environ.get("DLV_COMPARE_END") or date.today().isoformat()
    start = (date.fromisoformat(end) - timedelta(days=int(365.25 * years))).isoformat()
    sell_rank = resolve_sell_rank(TOP_N_BUY)
    print(f"prepare context {start} ~ {end} …")
    t0 = time.perf_counter()
    ctx = prepare_backtest_context(start, end, rebalance_days=1, verbose=True)
    print(f"context ready in {time.perf_counter() - t0:.0f}s")

    m0 = run(
        "baseline_no_risk",
        {
            "DLV_RISK_FILTER_ENABLED": "false",
            "DLV_OCF_QUALITY_FILTER_ENABLED": "false",
        },
        ctx,
        start,
        end,
        sell_rank,
    )
    m1 = run(
        "with_risk",
        {
            "DLV_RISK_FILTER_ENABLED": "true",
            "DLV_OCF_QUALITY_FILTER_ENABLED": "true",
        },
        ctx,
        start,
        end,
        sell_rank,
    )
    d = (m1.get("total_return_pct") or 0) - (m0.get("total_return_pct") or 0)
    print(f"delta ret: {d:+.2f}%")
