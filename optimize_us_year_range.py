"""美股近1年区间放宽回测（2010至今）：评估 V 型反弹漏买修复效果。

若放宽后组合利润/收益明显改善，且买入次数大幅增加，则配合分档 + 降额控制总投入。
"""

import contextlib
from copy import deepcopy

import config
from backtest_buy_signals import BacktestPanels, US_INDEX_META, _us_buy_snapshot
from backtest_trade_signals import _trade_totals, backtest_all, simulate_trades
from buy_amount_tiers import TIER_SCHEMES, format_tier_table
from config import (
    BACKTEST_OUTPUT_DIR,
    BUY_AMOUNT_BASE_BY_CODE,
    resolve_backtest_amounts,
)
from market_data import configure_stdout_utf8

START = "2010-01-01"
END = None  # 至今

CURRENT_US = {
    "NDX_BUY_FORWARD_PE_PERCENTILE_MAX": config.NDX_BUY_FORWARD_PE_PERCENTILE_MAX,
    "NDX_BUY_TRAILING_PE_PERCENTILE_MAX": config.NDX_BUY_TRAILING_PE_PERCENTILE_MAX,
    "NDX_BUY_MAX_YEAR_RANGE_PCT": config.NDX_BUY_MAX_YEAR_RANGE_PCT,
    "NDX_BUY_MAX_ABOVE_LOW_PCT": config.NDX_BUY_MAX_ABOVE_LOW_PCT,
    "NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": config.NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT,
    "SPX_BUY_FORWARD_PE_PERCENTILE_MAX": config.SPX_BUY_FORWARD_PE_PERCENTILE_MAX,
    "SPX_BUY_TRAILING_PE_PERCENTILE_MAX": config.SPX_BUY_TRAILING_PE_PERCENTILE_MAX,
    "SPX_BUY_MAX_YEAR_RANGE_PCT": config.SPX_BUY_MAX_YEAR_RANGE_PCT,
    "SPX_BUY_MAX_ABOVE_LOW_PCT": config.SPX_BUY_MAX_ABOVE_LOW_PCT,
    "SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": config.SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT,
}

# 主要测试：仅放宽近1年区间（针对 V 型反弹漏买）
RANGE_VARIANTS = {
    "current": {},
    "range_62": {
        "NDX_BUY_MAX_YEAR_RANGE_PCT": 0.62,
        "SPX_BUY_MAX_YEAR_RANGE_PCT": 0.62,
    },
    "range_65": {
        "NDX_BUY_MAX_YEAR_RANGE_PCT": 0.65,
        "SPX_BUY_MAX_YEAR_RANGE_PCT": 0.65,
    },
    "range_68": {
        "NDX_BUY_MAX_YEAR_RANGE_PCT": 0.68,
        "SPX_BUY_MAX_YEAR_RANGE_PCT": 0.68,
    },
    # 区间放宽 + 要求距高点回撤（防追高）
    "range_65_dd6": {
        "NDX_BUY_MAX_YEAR_RANGE_PCT": 0.65,
        "SPX_BUY_MAX_YEAR_RANGE_PCT": 0.65,
        "NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.06,
        "SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.04,
    },
    # 综合温和放宽（沿用 optimize_us_fx_quota mild 档）
    "mild_all": {
        "NDX_BUY_FORWARD_PE_PERCENTILE_MAX": 87,
        "NDX_BUY_TRAILING_PE_PERCENTILE_MAX": 89,
        "NDX_BUY_MAX_YEAR_RANGE_PCT": 0.62,
        "NDX_BUY_MAX_ABOVE_LOW_PCT": 0.21,
        "NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.06,
        "SPX_BUY_FORWARD_PE_PERCENTILE_MAX": 89,
        "SPX_BUY_TRAILING_PE_PERCENTILE_MAX": 89,
        "SPX_BUY_MAX_YEAR_RANGE_PCT": 0.63,
        "SPX_BUY_MAX_ABOVE_LOW_PCT": 0.19,
        "SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.04,
    },
}

TIER_OPTIONS = ("range_6_fine", "range_8_fine")
AMOUNT_SCALE_OPTIONS = {
    "base": 1.00,
    "low": 0.85,
    "lower": 0.72,
    "lowest": 0.60,
}


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


def _us_stats(panels, key, amount, tier_scheme="range_6_fine"):
    daily, growth = panels.us_index_panel(key)
    meta = US_INDEX_META[key]
    buy_fn = lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g)
    amounts = resolve_backtest_amounts(tier_enabled=True)
    by_code = dict(amounts["by_code"])
    by_code[meta["code"]] = amount
    amounts["by_code"] = by_code
    amounts["tier_scheme"] = tier_scheme
    amounts["tier_normalize"] = True
    from buy_amount_config import resolve_simulate_amount

    sim_amt = resolve_simulate_amount(
        meta["code"], amount, amounts, daily, START, END, buy_fn, "date"
    )
    st = simulate_trades(daily, START, END, amount=sim_amt, buy_fn=buy_fn)
    if not st:
        return None
    return {
        "code": meta["code"],
        "buy_count": st["buy_count"],
        "total_bought": st["total_bought"],
        "return_pct": st["buy_only_return_pct"],
        "profit": st["buy_only_profit"],
        "avg_amount": st["total_bought"] / st["buy_count"] if st["buy_count"] else 0,
    }


def _amounts_with_us(ndx_amt, spx_amt, tier_scheme):
    base = resolve_backtest_amounts(tier_enabled=True)
    by_code = dict(base["by_code"])
    by_code["NDX"] = ndx_amt
    by_code["SPX"] = spx_amt
    base["by_code"] = by_code
    base["tier_scheme"] = tier_scheme
    base["tier_normalize"] = True
    return base


def _portfolio_run(panels, amounts, us_overrides=None):
    merged = {**CURRENT_US, **(us_overrides or {})}
    with _patch_config(merged):
        return _trade_totals(backtest_all(START, END, amounts=amounts, panels=panels))


def _scaled_amount(base, scale, cap=None):
    amt = round(base * scale)
    if cap is not None:
        amt = min(amt, cap)
    return max(30, amt)


def _end_label():
    return END or "至今"


def main():
    configure_stdout_utf8()
    panels = BacktestPanels()
    ndx_base = BUY_AMOUNT_BASE_BY_CODE["NDX"]
    spx_base = BUY_AMOUNT_BASE_BY_CODE["SPX"]
    baseline_amounts = _amounts_with_us(ndx_base, spx_base, "range_6_fine")

    print(f"=== 美股近1年区间放宽回测 {START} ~ {_end_label()} ===\n")

    with _patch_config(CURRENT_US):
        ndx0 = _us_stats(panels, "ndx", ndx_base)
        spx0 = _us_stats(panels, "spx", spx_base)
    base_port = _portfolio_run(panels, baseline_amounts)
    base_buys = ndx0["buy_count"] + spx0["buy_count"]
    us_baseline = ndx0["total_bought"] + spx0["total_bought"]

    print("基线（当前阈值 + range_6_fine）：")
    print(
        f"  NDX: {ndx0['buy_count']}次, 投入 {ndx0['total_bought']:,.0f}元, "
        f"收益 {ndx0['return_pct']:+.1f}%, 利润 {ndx0['profit']:+,.0f}元"
    )
    print(
        f"  SPX: {spx0['buy_count']}次, 投入 {spx0['total_bought']:,.0f}元, "
        f"收益 {spx0['return_pct']:+.1f}%, 利润 {spx0['profit']:+,.0f}元"
    )
    print(
        f"  全组合: 投入 {base_port['total_bought']:,.0f}元, "
        f"收益 {base_port['trade_ret']:+.2f}%, 利润 {base_port['trade_profit']:+,.0f}元\n"
    )

    # 阶段1：仅放宽阈值，固定金额
    print("--- 阶段1：仅放宽阈值（金额不变）---")
    print(f"{'方案':<14} {'NDX次':>6} {'SPX次':>6} {'美股投入':>12} {'NDX收益':>8} {'组合利润':>12} {'Δ利润':>10}")
    print("-" * 80)
    stage1 = []
    for vname, overrides in RANGE_VARIANTS.items():
        with _patch_config({**CURRENT_US, **overrides}):
            ndx_s = _us_stats(panels, "ndx", ndx_base)
            spx_s = _us_stats(panels, "spx", spx_base)
        port = _portfolio_run(panels, baseline_amounts, overrides)
        us_inv = ndx_s["total_bought"] + spx_s["total_bought"]
        delta = (port["trade_profit"] or 0) - (base_port["trade_profit"] or 0)
        buys = ndx_s["buy_count"] + spx_s["buy_count"]
        stage1.append({
            "variant": vname,
            "overrides": overrides,
            "ndx": ndx_s,
            "spx": spx_s,
            "buys": buys,
            "us_invested": us_inv,
            "port": port,
            "profit_delta": delta,
            "return_delta": (port["trade_ret"] or 0) - (base_port["trade_ret"] or 0),
        })
        print(
            f"{vname:<14} {ndx_s['buy_count']:>6} {spx_s['buy_count']:>6} "
            f"{us_inv:>12,.0f} {ndx_s['return_pct']:>+7.1f}% "
            f"{port['trade_profit']:>+11,.0f} {delta:>+9,.0f}"
        )

    # 阶段2：放宽 + 分档 + 降额
    print(f"\n--- 阶段2：放宽 × 分档 × 降额（全组合回测）---")
    results = []
    for s1 in stage1:
        overrides = s1["overrides"]
        with _patch_config({**CURRENT_US, **overrides}):
            ndx_s = _us_stats(panels, "ndx", ndx_base)
            spx_s = _us_stats(panels, "spx", spx_base)
        for tier in TIER_OPTIONS:
            for scale_name, scale in AMOUNT_SCALE_OPTIONS.items():
                ndx_amt = _scaled_amount(ndx_base, scale, cap=750)
                spx_amt = _scaled_amount(spx_base, scale, cap=180)
                amounts = _amounts_with_us(ndx_amt, spx_amt, tier)
                port = _portfolio_run(panels, amounts, overrides)
                us_inv = ndx_s["buy_count"] * ndx_amt + spx_s["buy_count"] * spx_amt
                results.append({
                    "variant": s1["variant"],
                    "tier": tier,
                    "scale": scale_name,
                    "ndx_amt": ndx_amt,
                    "spx_amt": spx_amt,
                    "ndx_buys": ndx_s["buy_count"],
                    "spx_buys": spx_s["buy_count"],
                    "buys": ndx_s["buy_count"] + spx_s["buy_count"],
                    "us_invested": us_inv,
                    "us_invest_delta": us_inv - us_baseline,
                    "port_return": port["trade_ret"],
                    "port_profit": port["trade_profit"],
                    "profit_delta": (port["trade_profit"] or 0) - (base_port["trade_profit"] or 0),
                    "return_delta": (port["trade_ret"] or 0) - (base_port["trade_ret"] or 0),
                    "overrides": overrides,
                })

    # 筛选：利润不降超过1%，或利润提升；买入次数增加；总投入偏差可控
    feasible = [
        r for r in results
        if r["profit_delta"] >= -abs(base_port["trade_profit"] or 1) * 0.01
        and r["buys"] > base_buys
        and abs(r["us_invest_delta"]) <= max(us_baseline * 0.25, 50_000)
    ]
    feasible.sort(
        key=lambda r: (r["profit_delta"], r["buys"], r["return_delta"]),
        reverse=True,
    )

    print(f"{'放宽':<12} {'分档':<14} {'缩放':<7} {'NDX次/元':>12} {'SPX次/元':>12} "
          f"{'组合利润':>11} {'Δ利润':>9} {'Δ收益':>7} {'美股投入Δ':>10}")
    print("-" * 105)
    for r in feasible[:15]:
        print(
            f"{r['variant']:<12} {r['tier']:<14} {r['scale']:<7} "
            f"{r['ndx_buys']:>4}/{r['ndx_amt']:<6} "
            f"{r['spx_buys']:>4}/{r['spx_amt']:<6} "
            f"{r['port_profit']:>+10,.0f} {r['profit_delta']:>+8,.0f} "
            f"{r['return_delta']:>+6.2f}pp {r['us_invest_delta']:>+9,.0f}"
        )

    if feasible:
        best = feasible[0]
    else:
        # 退而求其次：利润最高
        best = max(results, key=lambda r: r["port_profit"] or 0)
        print("\n⚠ 无完全满足约束的方案，取利润最高：")

    print(f"\n推荐方案: {best['variant']} + {best['tier']} + 金额×{best['scale']}")
    print(
        f"  NDX: {best['ndx_buys']}次 × {best['ndx_amt']}元 "
        f"(基线 {ndx0['buy_count']}次 × {ndx_base}元, +{best['ndx_buys']-ndx0['buy_count']}次)"
    )
    print(
        f"  SPX: {best['spx_buys']}次 × {best['spx_amt']}元 "
        f"(基线 {spx0['buy_count']}次 × {spx_base}元, +{best['spx_buys']-spx0['buy_count']}次)"
    )
    print(
        f"  全组合利润 {best['port_profit']:+,.0f}元 "
        f"({best['profit_delta']:+,.0f}元 vs 基线 {base_port['trade_profit']:+,.0f}元)"
    )
    print(f"  全组合收益 {best['port_return']:+.2f}% ({best['return_delta']:+.2f}pp)")

    # 2026年3月漏买验证
    print("\n--- 2026年3月 NDX 买入验证 ---")
    daily, growth = panels.us_index_panel("ndx")
    from us_index_signal import evaluate_signal, resolve_expected_growth

    def _is_buy(row, overrides):
        snap = {
            "forward_pe_percentile": row.get("forward_pe_percentile"),
            "trailing_pe_percentile": row.get("trailing_pe_percentile"),
            "us10y_percentile": row.get("us10y_percentile"),
            "historical_growth": growth,
            "year_range_position": row.get("year_range_position"),
            "ma_slope_pct": row.get("ma_slope_pct"),
            "implied_growth": row.get("implied_growth"),
            "pct_above_low": row.get("pct_above_low"),
            "pct_below_high": row.get("pct_below_high"),
        }
        snap["expected_growth"] = resolve_expected_growth("ndx", snap)
        with _patch_config({**CURRENT_US, **overrides}):
            return evaluate_signal("ndx", snap)["is_buy"]

    march = daily[(daily["date"] >= "2026-03-01") & (daily["date"] <= "2026-03-31")]
    for vname in ("current", "range_62", "range_65", "range_68"):
        overrides = RANGE_VARIANTS[vname]
        buys = sum(1 for _, r in march.iterrows() if _is_buy(r, overrides))
        print(f"  {vname}: 3月买入 {buys} 天")

    # 写报告
    out = BACKTEST_OUTPUT_DIR / "us_year_range_2010_present.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# 美股近1年区间放宽回测（{START} ~ {_end_label()}）",
        "",
        "> 目标：修复 V 型反弹漏买；次数增加时配合分档降额。",
        "",
        "## 基线",
        "",
        f"| 指数 | 买入次 | 单次（元） | 总投入 | 收益率 |",
        f"| --- | ---: | ---: | ---: | ---: |",
        f"| NDX | {ndx0['buy_count']} | {ndx_base} | {ndx0['total_bought']:,.0f} | {ndx0['return_pct']:+.1f}% |",
        f"| SPX | {spx0['buy_count']} | {spx_base} | {spx0['total_bought']:,.0f} | {spx0['return_pct']:+.1f}% |",
        f"| **全组合** | — | — | {base_port['total_bought']:,.0f} | "
        f"利润 **{base_port['trade_profit']:+,.0f}** / 收益 **{base_port['trade_ret']:+.2f}%** |",
        "",
        "## 阶段1：仅放宽阈值",
        "",
        "| 方案 | NDX次 | SPX次 | 组合利润 | Δ利润 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for s in stage1:
        lines.append(
            f"| {s['variant']} | {s['ndx']['buy_count']} | {s['spx']['buy_count']} | "
            f"{s['port']['trade_profit']:+,.0f} | {s['profit_delta']:+,.0f} |"
        )
    lines += [
        "",
        "## 推荐方案",
        "",
        f"**`{best['variant']}`** + **`{best['tier']}`** + 金额缩放 **×{AMOUNT_SCALE_OPTIONS[best['scale']]:.0%}**",
        "",
        f"| 指数 | 买入次 | 单次基准（元） | 较基线 |",
        f"| --- | ---: | ---: | --- |",
        f"| NDX | {best['ndx_buys']} | {best['ndx_amt']} | "
        f"+{best['ndx_buys']-ndx0['buy_count']}次 |",
        f"| SPX | {best['spx_buys']} | {best['spx_amt']} | "
        f"+{best['spx_buys']-spx0['buy_count']}次 |",
        f"| **全组合** | — | — | 利润 **{best['port_profit']:+,.0f}** "
        f"({best['profit_delta']:+,.0f}元) |",
        "",
        "### 阈值变更",
        "",
        "```",
    ]
    for k, v in sorted(best["overrides"].items()):
        lines.append(f"{k}={v}")
    lines += [
        "```",
        "",
        f"### 分档方案 `{best['tier']}`",
        "",
        format_tier_table(best["tier"]),
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写入 {out}")

    return best, base_port, ndx0, spx0


if __name__ == "__main__":
    main()
