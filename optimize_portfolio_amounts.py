"""组合仓位下优化各指数单次买入金额（总投入不变）。"""

import itertools
from copy import deepcopy

from backtest_trade_signals import backtest_all, _trade_totals
from backtest_buy_signals import BacktestPanels
from config import (
    PORTFOLIO_GROUP_WEIGHTS,
    PORTFOLIO_INDEX_GROUPS,
    PORTFOLIO_TOTAL_BUDGET,
    _build_portfolio_by_code,
    resolve_backtest_amounts,
)
from market_data import configure_stdout_utf8

START = "2016-01-01"
END = "2025-12-31"


def _baseline_uniform_amounts():
    """全指数统一金额（非组合）。"""
    return {
        "dividend": 300,
        "cn_broad": 100,
        "other": 300,
        "unified": False,
        "portfolio": False,
        "by_code": None,
    }


def _portfolio_amounts(by_code):
    base = resolve_backtest_amounts(portfolio_mode=True)
    base["by_code"] = dict(by_code)
    return base


def _collect_stats(results):
    totals = _trade_totals(results)
    by_code = {r.code: r for r in results}
    return {
        "return_pct": totals["trade_ret"],
        "total_bought": totals["total_bought"],
        "profit": totals["trade_profit"],
        "buy_count": totals["buy_count"],
        "by_code": by_code,
    }


def _run(amounts, panels):
    results = backtest_all(START, END, amounts=amounts, panels=panels)
    return _collect_stats(results)


def _buy_counts_from_results(results):
    return {r.code: r.buy_count for r in results}


def _returns_from_results(results):
    out = {}
    for r in results:
        ret = r.return_pct if r.has_sell else r.buy_only_return_pct
        out[r.code] = ret or 0.0
    return out


def _equal_within_groups(buy_counts):
    by_code = {}
    group_codes = {}
    for code, group in PORTFOLIO_INDEX_GROUPS.items():
        group_codes.setdefault(group, []).append(code)
    for group, weight in PORTFOLIO_GROUP_WEIGHTS.items():
        codes = [c for c in group_codes.get(group, []) if buy_counts.get(c, 0) > 0]
        if not codes:
            continue
        budget = PORTFOLIO_TOTAL_BUDGET * weight
        per = budget / sum(buy_counts[c] for c in codes)
        for c in codes:
            by_code[c] = round(per)
    return by_code


def _return_weighted(buy_counts, index_returns):
    raw = _build_portfolio_by_code(buy_counts, index_returns, PORTFOLIO_TOTAL_BUDGET)
    return {c: round(v) for c, v in raw.items() if v > 0}


def _corner_optimal(buy_counts, index_returns):
    """组内全部预算给收益率最高的一只（极端）。"""
    by_code = {c: 0 for c in buy_counts}
    group_codes = {}
    for code, group in PORTFOLIO_INDEX_GROUPS.items():
        group_codes.setdefault(group, []).append(code)
    for group, weight in PORTFOLIO_GROUP_WEIGHTS.items():
        codes = [c for c in group_codes.get(group, []) if buy_counts.get(c, 0) > 0]
        if not codes:
            continue
        budget = PORTFOLIO_TOTAL_BUDGET * weight
        best = max(codes, key=lambda c: index_returns.get(c, 0))
        by_code[best] = round(budget / buy_counts[best])
    return by_code


def _grid_us_satellite(
    buy_counts,
    index_returns,
    us_ndx_share=0.72,
    sat_cyb_share=0.68,
    weights=None,
):
    """在组预算固定下，对美股/卫星做两档倾斜。"""
    group_weights = weights or PORTFOLIO_GROUP_WEIGHTS
    by_code = {c: 0 for c in buy_counts}
    group_codes = {}
    for code, group in PORTFOLIO_INDEX_GROUPS.items():
        group_codes.setdefault(group, []).append(code)
    for group, weight in group_weights.items():
        codes = [c for c in group_codes.get(group, []) if buy_counts.get(c, 0) > 0]
        if not codes:
            continue
        budget = PORTFOLIO_TOTAL_BUDGET * weight
        if group == "us" and len(codes) == 2:
            ndx, spx = "NDX", "SPX"
            by_code[ndx] = round(budget * us_ndx_share / buy_counts[ndx])
            by_code[spx] = round(budget * (1 - us_ndx_share) / buy_counts[spx])
        elif group == "satellite" and len(codes) == 2:
            cyb, zz = "399006", "000852"
            by_code[cyb] = round(budget * sat_cyb_share / buy_counts[cyb])
            by_code[zz] = round(budget * (1 - sat_cyb_share) / buy_counts[zz])
        elif group == "core":
            # 组内按收益加权
            denom = sum(buy_counts[c] * max(index_returns.get(c, 0), 0.01) for c in codes)
            for c in codes:
                r = max(index_returns.get(c, 0), 0.01)
                by_code[c] = round(budget * r / denom)
        elif len(codes) == 1:
            c = codes[0]
            by_code[c] = round(budget / buy_counts[c])
    return by_code


def _scale_to_budget(by_code, buy_counts, budget):
    total = sum(buy_counts.get(c, 0) * by_code.get(c, 0) for c in by_code)
    if total <= 0:
        return by_code
    factor = budget / total
    return {c: round(by_code.get(c, 0) * factor) for c in by_code}


def main():
    configure_stdout_utf8()
    panels = BacktestPanels()

    # 基准：全指数统一金额
    base_amt = _baseline_uniform_amounts()
    base_results = backtest_all(START, END, amounts=base_amt, panels=panels)
    base = _collect_stats(base_results)
    buy_counts = _buy_counts_from_results(base_results)
    # 仅用组合内指数的收益做组内加权
    returns = _returns_from_results(base_results)

    portfolio_returns = {
        c: returns[c]
        for c in PORTFOLIO_INDEX_GROUPS
        if c in returns
    }
    portfolio_buys = {
        c: buy_counts[c]
        for c in PORTFOLIO_INDEX_GROUPS
        if c in buy_counts
    }

    schemes = {
        "equal_within_group": _equal_within_groups(portfolio_buys),
        "return_weighted": _return_weighted(portfolio_buys, portfolio_returns),
        "corner_max": _corner_optimal(portfolio_buys, portfolio_returns),
    }

    # 网格：美股 NDX 占比 60–85%，卫星 CYB 占比 55–80%
    best_grid = None
    best_grid_ret = -1
    for us_share in (0.60, 0.65, 0.70, 0.72, 0.75, 0.80, 0.85):
        for sat_share in (0.55, 0.60, 0.65, 0.68, 0.70, 0.75, 0.80):
            code = _grid_us_satellite(
                portfolio_buys, portfolio_returns, us_share, sat_share
            )
            code = _scale_to_budget(code, portfolio_buys, PORTFOLIO_TOTAL_BUDGET)
            st = _run(_portfolio_amounts(code), panels)
            if st["return_pct"] and st["return_pct"] > best_grid_ret:
                best_grid_ret = st["return_pct"]
                best_grid = (us_share, sat_share, code, st)

    if best_grid:
        us_s, sat_s, code, st = best_grid
        schemes[f"grid_us{us_s:.0%}_sat{sat_s:.0%}"] = code

    # 组间权重扫描（在组合指数不变前提下微调）
    weight_schemes = {}
    for us_w in (0.18, 0.20, 0.22, 0.25, 0.28):
        for core_w in (0.42, 0.45, 0.50, 0.52):
            kc_w = 0.10
            sat_w = round(1.0 - us_w - core_w - kc_w, 4)
            if sat_w < 0.12:
                continue
            weights = {"core": core_w, "us": us_w, "kc50": kc_w, "satellite": sat_w}
            code = _grid_us_satellite(
                portfolio_buys, portfolio_returns, 0.82, 0.72, weights=weights
            )
            code = _scale_to_budget(code, portfolio_buys, PORTFOLIO_TOTAL_BUDGET)
            weight_schemes[f"w_core{core_w:.0%}_us{us_w:.0%}"] = code

    schemes.update(weight_schemes)

    print(f"\n=== 组合优化（总预算 {PORTFOLIO_TOTAL_BUDGET:,.0f} 元，{START} ~ {END}）===\n")
    print(
        f"{'方案':<28} {'总投入':>10} {'收益率':>8} {'利润':>10} {'买入次':>6}"
    )
    print("-" * 70)
    print(
        f"{'基准(全指数统一金额)':<28} "
        f"{base['total_bought']:>10,.0f} {base['return_pct']:>+7.1f}% "
        f"{base['profit']:>+10,.0f} {base['buy_count']:>6}"
    )

    best_name = None
    best = None
    best_ret = base["return_pct"] or 0

    for name, by_code in schemes.items():
        by_code = _scale_to_budget(by_code, portfolio_buys, PORTFOLIO_TOTAL_BUDGET)
        for c in buy_counts:
            if c not in PORTFOLIO_INDEX_GROUPS:
                by_code[c] = 0
        st = _run(_portfolio_amounts(by_code), panels)
        print(
            f"{name:<28} {st['total_bought']:>10,.0f} {st['return_pct']:>+7.1f}% "
            f"{st['profit']:>+10,.0f} {st['buy_count']:>6}"
        )
        if st["return_pct"] and st["return_pct"] > best_ret:
            best_ret = st["return_pct"]
            best_name = name
            best = by_code

    if best_grid and best_grid_ret > best_ret:
        best_name = f"grid_us{best_grid[0]:.0%}_sat{best_grid[1]:.0%}"
        best = best_grid[2]
        best_ret = best_grid_ret

    print("-" * 70)
    if best is None:
        best = {}
        best_name = "baseline_legacy"
    print(f"\n最优方案: {best_name}，收益率 {best_ret:+.1f}%")
    if best_name != "baseline_legacy":
        print("（组合约束下未超过全指数统一金额基准；以下为组合内最优金额）\n")
    else:
        print("（全指数统一金额仍为收益最高；以下为组合分指数金额供参考）\n")
    print("建议写入 config.py 的 BACKTEST_BUY_AMOUNT_BY_CODE：")
    for code in sorted(best.keys()):
        if code in PORTFOLIO_INDEX_GROUPS or code in ("000300", "000905", "HSTECH"):
            print(f'    "{code}": {best.get(code, 0)},')

    invested = sum(portfolio_buys.get(c, 0) * best.get(c, 0) for c in portfolio_buys)
    print(f"\n实际总投入: {invested:,.0f} 元（目标 {PORTFOLIO_TOTAL_BUDGET:,.0f}）")


if __name__ == "__main__":
    main()
