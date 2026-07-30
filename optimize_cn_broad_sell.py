"""单指数卖点参数快速搜索。"""

import argparse
import sys
from copy import deepcopy

from backtest_buy_signals import BacktestPanels
from backtest_trade_signals import _cn_broad_signals, _filter_panel
from cn_broad_signal import evaluate_cn_broad_buy
from config import _CN_BROAD_PER_INDEX_DEFAULTS, get_cn_broad_signal_config
from index_meta import get_index_base_date
from sell_trailing import simulate_trades_trailing, valuation_sell_hit_cn_broad


def _make_valuation_sell_fn(code, cfg_override):
    cfg = {**get_cn_broad_signal_config(code), **cfg_override}

    def _sell(row):
        snap = {
            "code": code,
            "pe_percentile": row.get("pe_percentile"),
            "pb_percentile": row.get("pb_percentile"),
            "spread_percentile": row.get("spread_percentile"),
            "pct_above_low": row.get("pct_above_low"),
            "year_range_position": row.get("year_range_position"),
        }
        buy_ev = evaluate_cn_broad_buy(
            {
                **snap,
                "pct_below_high": row.get("pct_below_high"),
                "year_range_position": row.get("year_range_position"),
                "ma_slope_pct": row.get("ma_slope_pct"),
            }
        )
        return valuation_sell_hit_cn_broad(snap, cfg) and not buy_ev["is_buy"]

    return _sell


def _run_sim(panel, code, overrides, start_date):
    buy_fn = lambda r, c=code: _cn_broad_signals(r, c)[0]
    trail_dd = overrides.get("sell_trailing_drawdown_pct")
    trail_gain = overrides.get("sell_min_unrealized_gain_pct")
    trail_hold = overrides.get("sell_trailing_min_hold_days", 60)
    trail_cfg = None
    if trail_dd is not None and trail_gain is not None:
        trail_cfg = {
            "trailing_drawdown_pct": trail_dd,
            "min_unrealized_gain_pct": trail_gain,
            "min_hold_days": trail_hold,
        }
    return simulate_trades_trailing(
        panel,
        start_date,
        amount=100.0,
        buy_fn=buy_fn,
        valuation_sell_fn=_make_valuation_sell_fn(code, overrides),
        trailing_cfg=trail_cfg,
        valuation_price_col="total_return_close",
    )


def _score(stats):
    if not stats or stats.get("total_bought", 0) <= 0:
        return -1e9, {}
    buy_only = stats.get("buy_only_return_pct") or 0
    sell_ret = stats.get("return_pct") or 0
    excess = sell_ret - buy_only
    sells = stats.get("sell_count", 0)
    if sells == 0:
        return excess - 5, {"excess": excess, "sell_ret": sell_ret, "sells": sells}
    if sells > 15:
        return excess - (sells - 15), {"excess": excess, "sell_ret": sell_ret, "sells": sells}
    return excess + sell_ret * 0.01, {"excess": excess, "sell_ret": sell_ret, "sells": sells}


def optimize(code, start_date, panel):
    best, best_score, best_meta = None, -1e9, {}
    base = deepcopy(_CN_BROAD_PER_INDEX_DEFAULTS[code])
    base_stats = _run_sim(
        panel, code, {**base, "sell_trailing_drawdown_pct": None}, start_date
    )
    buy_only = base_stats.get("buy_only_return_pct") if base_stats else None

    for trail_dd in [0.10, 0.12, None]:
        for trail_gain in [0.40, 0.50, 0.60]:
            for pe_min in [82, 88]:
                for range_min in [None, 0.88]:
                    for pe_combo in [None, 45, 55]:
                        for above_low in [0.24, 0.28]:
                            overrides = {
                                "sell_trailing_drawdown_pct": trail_dd,
                                "sell_min_unrealized_gain_pct": trail_gain,
                                "sell_trailing_min_hold_days": 60,
                                "sell_pe_percentile_min": pe_min,
                                "sell_spread_percentile_max": 25,
                                "sell_max_above_low_pct": above_low,
                                "sell_min_year_range_pct": range_min,
                                "sell_pe_combo_min": pe_combo,
                            }
                            stats = _run_sim(panel, code, overrides, start_date)
                            sc, meta = _score(stats)
                            if sc > best_score:
                                best_score = sc
                                best = overrides
                                best_meta = meta

    return best, best_meta, buy_only


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("code", help="指数代码")
    args = parser.parse_args()
    code = args.code
    start = get_index_base_date(code) or "20150101"
    start_date = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    panel = BacktestPanels().cn_broad_panel(code)
    if _filter_panel(panel, start_date, None).empty:
        print("无数据")
        return 1
    best, meta, buy_only = optimize(code, start_date, panel)
    print(f"code={code} buy_only={buy_only:.1f}%")
    print(f"best sell={meta.get('sell_ret'):.1f}% excess={meta.get('excess'):+.1f}% sells={meta.get('sells')}")
    for k, v in sorted(best.items()):
        if k.startswith("sell_"):
            print(f"  {k}={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
