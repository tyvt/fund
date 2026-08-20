# -*- coding: utf-8 -*-
"""逐日对比原生 nav 与 RQAlpha portfolio 净值，定位偏离日期。"""
from __future__ import annotations

import os
import pickle
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("DLV_BACKTEST_PRICE_SOURCE", "rqalpha")

from dividend_lowvol_rotation.backtest import run_backtest
from dividend_lowvol_rotation.config import BACKTEST_OUTPUT_DIR, TOP_N_BUY


def _default_start(years: int = 10) -> str:
    return (date.today() - timedelta(days=int(365.25 * years))).isoformat()


def main() -> None:
    start, end = _default_start(10), date.today().isoformat()
    pkl = BACKTEST_OUTPUT_DIR / "rqalpha_result.pkl"
    with pkl.open("rb") as f:
        data = pickle.load(f)
    cap = float(data.get("summary", {}).get("STOCK", 100_000))
    rq = data["portfolio"]["unit_net_value"].astype(float) * cap
    rq.index = pd.to_datetime(rq.index).normalize()

    nav, trades, _, _, _, _ = run_backtest(
        start=start, end=end, top_n=TOP_N_BUY, initial_capital=cap, verbose=False
    )
    nav["date"] = pd.to_datetime(nav["date"]).dt.normalize()
    nat = nav.sort_values("date").groupby("date", as_index=False).last().set_index("date")["nav"]

  # align
    common = nat.index.intersection(rq.index)
    diff = (rq.reindex(common) - nat.reindex(common)).dropna()
    print(f"区间 {start} ~ {end}，重叠 {len(common)} 日")
    print(f"最终：原生 {nat.iloc[-1]:,.2f}  RQ {rq.iloc[-1]:,.2f}  差 {rq.iloc[-1]-nat.iloc[-1]:+,.2f}")
    print(f"最大正偏：{diff.max():,.2f} @ {diff.idxmax().date()}")
    print(f"最大负偏：{diff.min():,.2f} @ {diff.idxmin().date()}")
    # rebalance-ish dates from trades
    trades["date"] = pd.to_datetime(trades["date"]).dt.normalize()
    rb_dates = sorted(trades.groupby("date").size().pipe(lambda s: s[s >= 3]).index)
    print("\n调仓日收盘净值对比：")
    for d in rb_dates:
        if d not in common:
            continue
        print(f"  {d.date()}  native {nat.loc[d]:,.2f}  rq {rq.loc[d]:,.2f}  Δ {rq.loc[d]-nat.loc[d]:+,.2f}")


if __name__ == "__main__":
    main()
