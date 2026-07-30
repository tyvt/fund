"""自基日扫描各指数移动止盈参数，找出超额收益明显的配置。"""

import sys
from itertools import product

from backtest_buy_signals import (
    BacktestPanels,
    CN_BROAD_BACKTEST_INDICES,
    INDICES,
    US_INDEX_META,
    _us_buy_snapshot,
)
from backtest_trade_signals import (
    _cn_broad_trailing_cfg,
    _cyb_trailing_cfg,
    _filter_panel,
    _resolve_trade_amount,
    simulate_trades,
)
from cn_broad_signal import evaluate_cn_broad_buy
from config import (
    CYB_INDEX,
    HSTECH_INDEX,
    US_INDEX_KEYS,
    get_backtest_buy_amount,
    get_cn_broad_signal_config,
    resolve_backtest_amounts,
)
from cyb_signal import evaluate_cyb_signal
from dividend_data import is_buy_signal_row
from hstech_signal import evaluate_hstech_signal
from index_meta import get_index_base_date_iso
from market_data import configure_stdout_utf8
from optimize_sell_signals import _cn_snap, _cyb_snap
from sell_trailing import simulate_trades_trailing

MIN_DELTA_PCT = 3.0  # 明显超额门槛


def _hstech_snap(row):
    return {
        "pe": row["pe"],
        "pe_percentile": row.get("pe_percentile"),
        "dividend_percentile": row.get("dividend_percentile"),
        "pct_above_low": row.get("pct_above_low"),
        "pct_below_high": row.get("pct_below_high"),
        "year_range_position": row.get("year_range_position"),
        "ma_slope_pct": row.get("ma_slope_pct"),
        "close": row.get("close"),
    }


def _sim_trail(panel, start, buy_fn, sim_amt, trail, gain, mh, date_col="date", val_col="close"):
    cfg = {
        "trailing_drawdown_pct": trail,
        "min_unrealized_gain_pct": gain,
        "min_hold_days": mh,
    }
    return simulate_trades_trailing(
        panel,
        start,
        amount=sim_amt,
        date_col=date_col,
        buy_fn=buy_fn,
        trailing_cfg=cfg,
        valuation_price_col=val_col,
    )


def _best_trail(panel, start, buy_fn, sim_amt, date_col="date", val_col="close"):
    hold = simulate_trades(
        panel, start, amount=sim_amt, buy_fn=buy_fn, has_sell=False,
        date_col=date_col, valuation_price_col=val_col,
    )
    if not hold or hold["buy_count"] < 3:
        return None
    best = None
    for trail, gain, mh in product(
        [0.10, 0.12, 0.14, 0.15, 0.18],
        [0.50, 0.60, 0.70, 0.80, 0.90, 1.0],
        [60, 90, 120],
    ):
        trade = _sim_trail(panel, start, buy_fn, sim_amt, trail, gain, mh, date_col, val_col)
        if not trade or trade["sell_count"] < 1:
            continue
        delta = (trade["return_pct"] or 0) - (hold["buy_only_return_pct"] or 0)
        if best is None or delta > best["delta"]:
            best = {
                "hold_ret": hold["buy_only_return_pct"],
                "trade_ret": trade["return_pct"],
                "delta": delta,
                "sells": trade["sell_count"],
                "sell_dates": trade["sell_dates"],
                "trail": trail,
                "gain": gain,
                "mh": mh,
            }
    if best:
        best["hold"] = hold
    return best


def scan_all():
    panels = BacktestPanels()
    amounts = resolve_backtest_amounts(tier_enabled=True)
    results = []

    for item in INDICES:
        code = item["code"]
        start = get_index_base_date_iso(code)
        panel = panels.dividend_panel(code)
        buy_fn = lambda r, c=code: is_buy_signal_row(r, c)
        amt = get_backtest_buy_amount(code, amounts)
        if amt <= 0:
            continue
        sim = _resolve_trade_amount(code, amt, amounts, panel, start, None, buy_fn)
        best = _best_trail(panel, start, buy_fn, sim, val_col="total_return_close")
        results.append(("dividend", code, item["name"], start, best))

    for item in CN_BROAD_BACKTEST_INDICES:
        code = item["code"]
        start = get_index_base_date_iso(code)
        panel = panels.cn_broad_panel(code)
        buy_fn = lambda r, c=code: evaluate_cn_broad_buy(_cn_snap(r, c))["is_buy"]
        amt = get_backtest_buy_amount(code, amounts)
        if amt <= 0:
            continue
        sim = _resolve_trade_amount(code, amt, amounts, panel, start, None, buy_fn)
        existing = _cn_broad_trailing_cfg(code)
        hold = simulate_trades(panel, start, amount=sim, buy_fn=buy_fn, has_sell=False)
        if existing:
            trade = simulate_trades_trailing(
                panel, start, amount=sim, buy_fn=buy_fn, trailing_cfg={
                    "trailing_drawdown_pct": existing["trailing_drawdown_pct"],
                    "min_unrealized_gain_pct": existing["min_unrealized_gain_pct"],
                    "min_hold_days": existing["min_hold_days"],
                },
            )
            if trade and hold:
                delta = (trade["return_pct"] or 0) - (hold["buy_only_return_pct"] or 0)
                best = {
                    "hold_ret": hold["buy_only_return_pct"],
                    "trade_ret": trade["return_pct"],
                    "delta": delta,
                    "sells": trade["sell_count"],
                    "sell_dates": trade["sell_dates"],
                    "trail": existing["trailing_drawdown_pct"],
                    "gain": existing["min_unrealized_gain_pct"],
                    "mh": existing["min_hold_days"],
                    "existing": True,
                }
            else:
                best = None
        else:
            best = _best_trail(panel, start, buy_fn, sim)
            if best:
                best["existing"] = False
        results.append(("cn_broad", code, item["name"], start, best))

    cyb_code = CYB_INDEX["code"]
    start = get_index_base_date_iso(cyb_code)
    panel = panels.cyb_panel()
    buy_fn = lambda r: evaluate_cyb_signal(_cyb_snap(r))["is_buy"]
    amt = get_backtest_buy_amount(cyb_code, amounts)
    sim = _resolve_trade_amount(cyb_code, amt, amounts, panel, start, None, buy_fn)
    existing = _cyb_trailing_cfg()
    hold = simulate_trades(panel, start, amount=sim, buy_fn=buy_fn, has_sell=False)
    trade = simulate_trades_trailing(
        panel, start, amount=sim, buy_fn=buy_fn, trailing_cfg=existing,
    )
    delta = (trade["return_pct"] or 0) - (hold["buy_only_return_pct"] or 0)
    results.append(("cyb", cyb_code, CYB_INDEX["name"], start, {
        "hold_ret": hold["buy_only_return_pct"],
        "trade_ret": trade["return_pct"],
        "delta": delta,
        "sells": trade["sell_count"],
        "sell_dates": trade["sell_dates"],
        "trail": existing["trailing_drawdown_pct"],
        "gain": existing["min_unrealized_gain_pct"],
        "mh": existing["min_hold_days"],
        "existing": True,
    }))

    hs_code = HSTECH_INDEX["code"]
    start = get_index_base_date_iso(hs_code)
    panel = panels.hstech_panel()
    buy_fn = lambda r: evaluate_hstech_signal(_hstech_snap(r))["is_buy"]
    amt = get_backtest_buy_amount(hs_code, amounts)
    if amt > 0:
        sim = _resolve_trade_amount(hs_code, amt, amounts, panel, start, None, buy_fn)
        best = _best_trail(panel, start, buy_fn, sim)
        if best:
            best["existing"] = False
        results.append(("hstech", hs_code, HSTECH_INDEX["name"], start, best))

    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        meta = US_INDEX_META[key]
        start = get_index_base_date_iso(meta["code"])
        buy_fn = lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g)
        amt = get_backtest_buy_amount(meta["code"], amounts)
        if amt <= 0:
            continue
        sim = _resolve_trade_amount(meta["code"], amt, amounts, daily, start, None, buy_fn)
        best = _best_trail(daily, start, buy_fn, sim)
        if best:
            best["existing"] = False
        results.append(("us", meta["code"], meta["name"], start, best))

    return results


def main():
    configure_stdout_utf8()
    results = scan_all()
    print(f"=== 自基日移动止盈扫描（超额≥{MIN_DELTA_PCT:.0f}% 为明显）===\n")
    print(f"{'指数':<14} {'代码':<8} {'基日':<12} {'仅买':>8} {'波段':>8} {'超额':>7} {'卖':>3} 参数")
    print("-" * 95)
    winners = []
    for kind, code, name, start, best in results:
        if not best:
            print(f"{name:<14} {code:<8} {start:<12} {'—':>8} {'—':>8} {'—':>7} {'—':>3} 无有效卖点")
            continue
        flag = "OK" if best["delta"] >= MIN_DELTA_PCT else ("~" if best["delta"] > 0 else "X")
        ex = " [已有]" if best.get("existing") else ""
        print(
            f"{name:<14} {code:<8} {start:<12} "
            f"{best['hold_ret']:>+7.1f}% {best['trade_ret']:>+7.1f}% "
            f"{best['delta']:>+6.1f}% {best['sells']:>3} "
            f"trail={best['trail']:.0%} gain={best['gain']:.0%} mh={best['mh']}{ex} {flag}"
        )
        if best["delta"] >= MIN_DELTA_PCT:
            winners.append((kind, code, name, best))
    print(f"\n建议启用卖点（超额≥{MIN_DELTA_PCT:.0f}%）: {len(winners)} 只")
    for kind, code, name, best in winners:
        print(f"  {name} ({code}): trail={best['trail']}, gain={best['gain']}, mh={best['mh']}")
    return winners


if __name__ == "__main__":
    main()
