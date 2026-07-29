"""美股限购友好：放宽买入标准 + 降低单次金额，保持总投入与组合收益。"""

import contextlib
from copy import deepcopy

import config
from backtest_buy_signals import BacktestPanels, US_INDEX_META
from backtest_trade_signals import _trade_totals, backtest_all, simulate_trades
from backtest_buy_signals import _us_buy_snapshot
from config import (
    BACKTEST_OUTPUT_DIR,
    BUY_AMOUNT_BASE_BY_CODE,
    PORTFOLIO_TOTAL_BUDGET,
    resolve_backtest_amounts,
)
from market_data import configure_stdout_utf8

START = "2016-01-01"
END = "2025-12-31"

# 基线阈值（当前 config）
BASELINE_US = {
    "NDX_BUY_FORWARD_PE_PERCENTILE_MAX": 75,
    "NDX_BUY_TRAILING_PE_PERCENTILE_MAX": 78,
    "NDX_BUY_PEG_FORWARD_MAX": 1.45,
    "NDX_BUY_MAX_YEAR_RANGE_PCT": 0.45,
    "NDX_BUY_MAX_ABOVE_LOW_PCT": 0.14,
    "NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.10,
    "SPX_BUY_FORWARD_PE_PERCENTILE_MAX": 78,
    "SPX_BUY_TRAILING_PE_PERCENTILE_MAX": 78,
    "SPX_BUY_PEG_FORWARD_MAX": 1.35,
    "SPX_BUY_MAX_YEAR_RANGE_PCT": 0.48,
    "SPX_BUY_MAX_ABOVE_LOW_PCT": 0.12,
    "SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.08,
}

# 候选放宽方案（逐级）
RELAX_VARIANTS = {
    "mild": {
        "NDX_BUY_FORWARD_PE_PERCENTILE_MAX": 80,
        "NDX_BUY_TRAILING_PE_PERCENTILE_MAX": 82,
        "NDX_BUY_PEG_FORWARD_MAX": 1.55,
        "NDX_BUY_MAX_YEAR_RANGE_PCT": 0.52,
        "NDX_BUY_MAX_ABOVE_LOW_PCT": 0.16,
        "SPX_BUY_FORWARD_PE_PERCENTILE_MAX": 82,
        "SPX_BUY_TRAILING_PE_PERCENTILE_MAX": 82,
        "SPX_BUY_PEG_FORWARD_MAX": 1.45,
        "SPX_BUY_MAX_YEAR_RANGE_PCT": 0.54,
        "SPX_BUY_MAX_ABOVE_LOW_PCT": 0.14,
    },
    "moderate": {
        "NDX_BUY_FORWARD_PE_PERCENTILE_MAX": 83,
        "NDX_BUY_TRAILING_PE_PERCENTILE_MAX": 85,
        "NDX_BUY_PEG_FORWARD_MAX": 1.65,
        "NDX_BUY_MAX_YEAR_RANGE_PCT": 0.56,
        "NDX_BUY_MAX_ABOVE_LOW_PCT": 0.18,
        "NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.08,
        "SPX_BUY_FORWARD_PE_PERCENTILE_MAX": 85,
        "SPX_BUY_TRAILING_PE_PERCENTILE_MAX": 85,
        "SPX_BUY_PEG_FORWARD_MAX": 1.55,
        "SPX_BUY_MAX_YEAR_RANGE_PCT": 0.58,
        "SPX_BUY_MAX_ABOVE_LOW_PCT": 0.16,
        "SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.06,
    },
    "quota": {
        "NDX_BUY_FORWARD_PE_PERCENTILE_MAX": 86,
        "NDX_BUY_TRAILING_PE_PERCENTILE_MAX": 88,
        "NDX_BUY_PEG_FORWARD_MAX": 1.75,
        "NDX_BUY_MAX_YEAR_RANGE_PCT": 0.60,
        "NDX_BUY_MAX_ABOVE_LOW_PCT": 0.20,
        "NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.06,
        "SPX_BUY_FORWARD_PE_PERCENTILE_MAX": 88,
        "SPX_BUY_TRAILING_PE_PERCENTILE_MAX": 88,
        "SPX_BUY_PEG_FORWARD_MAX": 1.65,
        "SPX_BUY_MAX_YEAR_RANGE_PCT": 0.62,
        "SPX_BUY_MAX_ABOVE_LOW_PCT": 0.18,
        "SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.05,
    },
}

# 单次金额上限候选（元）
NDX_CAP_OPTIONS = (450, 550, 650)
SPX_CAP_OPTIONS = (120, 150, 180)


@contextlib.contextmanager
def _patch_config(overrides):
    old = {k: getattr(config, k) for k in overrides}
    for k, v in overrides.items():
        setattr(config, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(config, k, v)


def _us_stats(panels, key, amount):
    daily, growth = panels.us_index_panel(key)
    meta = US_INDEX_META[key]
    buy_fn = lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g)
    st = simulate_trades(daily, START, END, amount=amount, buy_fn=buy_fn)
    if not st:
        return None
    return {
        "code": meta["code"],
        "buy_count": st["buy_count"],
        "total_bought": st["total_bought"],
        "return_pct": st["buy_only_return_pct"],
        "profit": st["buy_only_profit"],
    }


def _scaled_amount(base_amt, base_buys, new_buys, cap=None):
    if new_buys <= 0:
        return base_amt
    amt = round(base_amt * base_buys / new_buys)
    if cap is not None:
        amt = min(amt, cap)
    return max(30, amt)


def _portfolio_run(panels, amounts, us_overrides=None):
    merged = {**BASELINE_US, **(us_overrides or {})}
    with _patch_config(merged):
        return _trade_totals(
            backtest_all(START, END, amounts=amounts, panels=panels)
        )


def _amounts_with_us(ndx_amt, spx_amt, tier=True):
    base = resolve_backtest_amounts(tier_enabled=tier)
    by_code = dict(base["by_code"])
    by_code["NDX"] = ndx_amt
    by_code["SPX"] = spx_amt
    base["by_code"] = by_code
    return base


def main():
    configure_stdout_utf8()
    panels = BacktestPanels()

    ndx_base = BUY_AMOUNT_BASE_BY_CODE["NDX"]
    spx_base = BUY_AMOUNT_BASE_BY_CODE["SPX"]
    baseline_amounts = resolve_backtest_amounts()

    print(f"=== 美股限购友好优化 {START}~{END} ===\n")
    with _patch_config(BASELINE_US):
        ndx0 = _us_stats(panels, "ndx", ndx_base)
        spx0 = _us_stats(panels, "spx", spx_base)
    base_port = _portfolio_run(panels, baseline_amounts)

    print("基线（当前阈值 + 金额）：")
    print(
        f"  NDX: {ndx0['buy_count']}次 × {ndx_base}元 = {ndx0['total_bought']:,.0f}元, "
        f"收益 {ndx0['return_pct']:+.1f}%"
    )
    print(
        f"  SPX: {spx0['buy_count']}次 × {spx_base}元 = {spx0['total_bought']:,.0f}元, "
        f"收益 {spx0['return_pct']:+.1f}%"
    )
    print(
        f"  全组合: 投入 {base_port['total_bought']:,.0f}元, "
        f"收益 {base_port['trade_ret']:+.2f}%, 利润 {base_port['trade_profit']:+,.0f}\n"
    )

    # 阶段1：仅统计美股买入次数（快）
    us_variants = {}
    for vname, overrides in RELAX_VARIANTS.items():
        with _patch_config({**BASELINE_US, **overrides}):
            us_variants[vname] = {
                "ndx": _us_stats(panels, "ndx", ndx_base),
                "spx": _us_stats(panels, "spx", spx_base),
                "overrides": {**BASELINE_US, **overrides},
            }

    # 阶段2：组合回测（仅各放宽档 × 金额网格）
    results = []
    for vname, data in us_variants.items():
        ndx_s, spx_s = data["ndx"], data["spx"]
        overrides = RELAX_VARIANTS[vname]
        for ndx_cap in NDX_CAP_OPTIONS:
            for spx_cap in SPX_CAP_OPTIONS:
                ndx_amt = _scaled_amount(
                    ndx_base, ndx0["buy_count"], ndx_s["buy_count"], ndx_cap
                )
                spx_amt = _scaled_amount(
                    spx_base, spx0["buy_count"], spx_s["buy_count"], spx_cap
                )
                amounts = _amounts_with_us(ndx_amt, spx_amt)
                port = _portfolio_run(panels, amounts, overrides)
                us_invested = (
                    ndx_s["buy_count"] * ndx_amt + spx_s["buy_count"] * spx_amt
                )
                us_baseline = ndx0["total_bought"] + spx0["total_bought"]
                results.append({
                    "variant": vname,
                    "ndx_cap": ndx_cap,
                    "spx_cap": spx_cap,
                    "ndx_amt": ndx_amt,
                    "spx_amt": spx_amt,
                    "ndx_buys": ndx_s["buy_count"],
                    "spx_buys": spx_s["buy_count"],
                    "us_invested": us_invested,
                    "us_invest_delta": us_invested - us_baseline,
                    "port_return": port["trade_ret"],
                    "port_profit": port["trade_profit"],
                    "port_bought": port["total_bought"],
                    "return_delta": (port["trade_ret"] or 0) - (base_port["trade_ret"] or 0),
                    "overrides": overrides,
                })

    # 筛选：组合收益降幅 < 3pp，美股总投入偏差 < 15%
    us_baseline = ndx0["total_bought"] + spx0["total_bought"]
    feasible = [
        r for r in results
        if r["return_delta"] >= -3.0
        and abs(r["us_invest_delta"]) <= us_baseline * 0.15
        and r["ndx_amt"] <= 600
        and r["spx_amt"] <= 180
    ]
    feasible.sort(
        key=lambda r: (
            -r["ndx_buys"] - r["spx_buys"],
            r["return_delta"],
            -r["ndx_amt"],
        ),
        reverse=True,
    )

    print("--- 可行方案 Top 10（收益降幅<3pp，美股投入偏差<15%，NDX≤600 SPX≤180）---")
    print(
        f"{'方案':<12} {'NDX次':>6} {'NDX元':>6} {'SPX次':>6} {'SPX元':>6} "
        f"{'组合收益':>8} {'Δ收益':>7} {'美股投入Δ':>10}"
    )
    print("-" * 72)
    for r in feasible[:10]:
        print(
            f"{r['variant']:<12} {r['ndx_buys']:>6} {r['ndx_amt']:>6} "
            f"{r['spx_buys']:>6} {r['spx_amt']:>6} "
            f"{r['port_return']:>+7.2f}% {r['return_delta']:>+6.2f}pp "
            f"{r['us_invest_delta']:>+10,.0f}"
        )

    best = feasible[0] if feasible else max(results, key=lambda r: r["port_return"])
    print(f"\n推荐方案: {best['variant']} + NDX≤{best['ndx_cap']} SPX≤{best['spx_cap']}")
    print(
        f"  NDX: {best['ndx_buys']}次 × {best['ndx_amt']}元 "
        f"(基线 {ndx0['buy_count']}次 × {ndx_base}元)"
    )
    print(
        f"  SPX: {best['spx_buys']}次 × {best['spx_amt']}元 "
        f"(基线 {spx0['buy_count']}次 × {spx_base}元)"
    )
    print(
        f"  全组合收益 {best['port_return']:+.2f}% "
        f"({best['return_delta']:+.2f}pp vs 基线 {base_port['trade_ret']:+.2f}%)"
    )

    lines = [
        f"# 美股限购友好优化（{START} ~ {END}）",
        "",
        "> 目标：放宽 NDX/SPX 买入标准、降低单次金额、增加买入次数，不明显降低全组合收益。",
        "",
        "## 基线",
        "",
        f"| 指数 | 买入次 | 单次（元） | 总投入 | 收益率 |",
        f"| --- | ---: | ---: | ---: | ---: |",
        f"| NDX | {ndx0['buy_count']} | {ndx_base} | {ndx0['total_bought']:,.0f} | {ndx0['return_pct']:+.1f}% |",
        f"| SPX | {spx0['buy_count']} | {spx_base} | {spx0['total_bought']:,.0f} | {spx0['return_pct']:+.1f}% |",
        f"| **全组合** | — | — | {base_port['total_bought']:,.0f} | **{base_port['trade_ret']:+.2f}%** |",
        "",
        "## 推荐方案",
        "",
        f"**{best['variant']}** + 单次上限 NDX {best['ndx_cap'] or '无'} / SPX {best['spx_cap'] or '无'}",
        "",
        f"| 指数 | 买入次 | 单次（元） | 较基线 |",
        f"| --- | ---: | ---: | --- |",
        f"| NDX | {best['ndx_buys']} | {best['ndx_amt']} | "
        f"+{best['ndx_buys']-ndx0['buy_count']}次 / {best['ndx_amt']-ndx_base:+d}元 |",
        f"| SPX | {best['spx_buys']} | {best['spx_amt']} | "
        f"+{best['spx_buys']-spx0['buy_count']}次 / {best['spx_amt']-spx_base:+d}元 |",
        f"| **全组合收益** | — | — | **{best['port_return']:+.2f}%** ({best['return_delta']:+.2f}pp) |",
        "",
        "### 阈值调整",
        "",
        "| 参数 | NDX 基线→推荐 | SPX 基线→推荐 |",
        "| --- | --- | --- |",
    ]
    for key in sorted(best["overrides"]):
        if key.startswith("NDX_"):
            spx_key = key.replace("NDX_", "SPX_")
            if spx_key in best["overrides"]:
                bv = BASELINE_US.get(key, "—")
                sv = BASELINE_US.get(spx_key, "—")
                nv = best["overrides"][key]
                nsv = best["overrides"][spx_key]
                lines.append(f"| {key.replace('NDX_BUY_', '')} | {bv} → {nv} | {sv} → {nsv} |")

    lines.extend(["", "### 可行方案 Top 10", ""])
    lines.append(
        "| 方案 | NDX次/元 | SPX次/元 | 组合收益 | Δ收益 | 美股投入Δ |"
    )
    lines.append("| --- | --- | --- | ---: | ---: | ---: |")
    for r in feasible[:10]:
        lines.append(
            f"| {r['variant']} | {r['ndx_buys']}/{r['ndx_amt']} | "
            f"{r['spx_buys']}/{r['spx_amt']} | {r['port_return']:+.2f}% | "
            f"{r['return_delta']:+.2f}pp | {r['us_invest_delta']:+,.0f} |"
        )

    path = BACKTEST_OUTPUT_DIR / "us_quota_friendly.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"\n报告: {path}")
    return best


if __name__ == "__main__":
    main()
