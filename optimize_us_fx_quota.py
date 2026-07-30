"""美股限购回测：适度放宽买入标准 + 更细分档金额，寻找最优规则。"""

import contextlib
from copy import deepcopy

import config
from backtest_buy_signals import BacktestPanels, US_INDEX_META, _us_buy_snapshot
from backtest_trade_signals import _trade_totals, backtest_all, simulate_trades
from buy_amount_tiers import TIER_SCHEMES, format_tier_table
from config import (
    BACKTEST_OUTPUT_DIR,
    BUY_AMOUNT_BASE_BY_CODE,
    PORTFOLIO_TOTAL_BUDGET,
    resolve_backtest_amounts,
)
from market_data import configure_stdout_utf8

START = "2016-01-01"
END = "2025-12-31"

# 当前 config 阈值（作为基线）
CURRENT_US = {
    "NDX_BUY_FORWARD_PE_PERCENTILE_MAX": config.NDX_BUY_FORWARD_PE_PERCENTILE_MAX,
    "NDX_BUY_TRAILING_PE_PERCENTILE_MAX": config.NDX_BUY_TRAILING_PE_PERCENTILE_MAX,
    "NDX_BUY_PEG_FORWARD_MAX": config.NDX_BUY_PEG_FORWARD_MAX,
    "NDX_BUY_MAX_YEAR_RANGE_PCT": config.NDX_BUY_MAX_YEAR_RANGE_PCT,
    "NDX_BUY_MAX_ABOVE_LOW_PCT": config.NDX_BUY_MAX_ABOVE_LOW_PCT,
    "NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": config.NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT,
    "SPX_BUY_FORWARD_PE_PERCENTILE_MAX": config.SPX_BUY_FORWARD_PE_PERCENTILE_MAX,
    "SPX_BUY_TRAILING_PE_PERCENTILE_MAX": config.SPX_BUY_TRAILING_PE_PERCENTILE_MAX,
    "SPX_BUY_PEG_FORWARD_MAX": config.SPX_BUY_PEG_FORWARD_MAX,
    "SPX_BUY_MAX_YEAR_RANGE_PCT": config.SPX_BUY_MAX_YEAR_RANGE_PCT,
    "SPX_BUY_MAX_ABOVE_LOW_PCT": config.SPX_BUY_MAX_ABOVE_LOW_PCT,
    "SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": config.SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT,
}

# 在现有基础上再适度放宽（限购场景）
RELAX_VARIANTS = {
    "current": {},
    "mild": {
        "NDX_BUY_FORWARD_PE_PERCENTILE_MAX": 87,
        "NDX_BUY_TRAILING_PE_PERCENTILE_MAX": 89,
        "NDX_BUY_PEG_FORWARD_MAX": 1.78,
        "NDX_BUY_MAX_YEAR_RANGE_PCT": 0.62,
        "NDX_BUY_MAX_ABOVE_LOW_PCT": 0.21,
        "NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.06,
        "SPX_BUY_FORWARD_PE_PERCENTILE_MAX": 89,
        "SPX_BUY_TRAILING_PE_PERCENTILE_MAX": 89,
        "SPX_BUY_PEG_FORWARD_MAX": 1.68,
        "SPX_BUY_MAX_YEAR_RANGE_PCT": 0.63,
        "SPX_BUY_MAX_ABOVE_LOW_PCT": 0.19,
        "SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.04,
    },
    "moderate": {
        "NDX_BUY_FORWARD_PE_PERCENTILE_MAX": 89,
        "NDX_BUY_TRAILING_PE_PERCENTILE_MAX": 90,
        "NDX_BUY_PEG_FORWARD_MAX": 1.85,
        "NDX_BUY_MAX_YEAR_RANGE_PCT": 0.65,
        "NDX_BUY_MAX_ABOVE_LOW_PCT": 0.23,
        "NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.05,
        "SPX_BUY_FORWARD_PE_PERCENTILE_MAX": 90,
        "SPX_BUY_TRAILING_PE_PERCENTILE_MAX": 90,
        "SPX_BUY_PEG_FORWARD_MAX": 1.75,
        "SPX_BUY_MAX_YEAR_RANGE_PCT": 0.65,
        "SPX_BUY_MAX_ABOVE_LOW_PCT": 0.21,
        "SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.03,
    },
    "quota": {
        "NDX_BUY_FORWARD_PE_PERCENTILE_MAX": 91,
        "NDX_BUY_TRAILING_PE_PERCENTILE_MAX": 92,
        "NDX_BUY_PEG_FORWARD_MAX": 1.92,
        "NDX_BUY_MAX_YEAR_RANGE_PCT": 0.68,
        "NDX_BUY_MAX_ABOVE_LOW_PCT": 0.25,
        "NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.04,
        "SPX_BUY_FORWARD_PE_PERCENTILE_MAX": 92,
        "SPX_BUY_TRAILING_PE_PERCENTILE_MAX": 92,
        "SPX_BUY_PEG_FORWARD_MAX": 1.82,
        "SPX_BUY_MAX_YEAR_RANGE_PCT": 0.68,
        "SPX_BUY_MAX_ABOVE_LOW_PCT": 0.23,
        "SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT": 0.02,
    },
}

TIER_OPTIONS = ("range_4_mild", "range_6_fine", "range_8_fine")

# 基准金额缩放系数（配合更细分档，降低单次上限）
AMOUNT_SCALE_OPTIONS = {
  "base": 1.00,
  "low": 0.85,
  "lower": 0.72,
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


def _us_stats(panels, key, amount):
    daily, growth = panels.us_index_panel(key)
    meta = US_INDEX_META[key]
    buy_fn = lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g)
    st = simulate_trades(
        daily, START, END, amount=amount, buy_fn=buy_fn
    )
    if not st:
        return None
    return {
        "code": meta["code"],
        "buy_count": st["buy_count"],
        "total_bought": st["total_bought"],
        "return_pct": st["buy_only_return_pct"],
        "profit": st["buy_only_profit"],
    }


def _amounts_with_us(ndx_amt, spx_amt, tier_scheme, tier=True):
    base = resolve_backtest_amounts(tier_enabled=tier)
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
        return _trade_totals(
            backtest_all(START, END, amounts=amounts, panels=panels)
        )


def _scaled_amount(base, scale, cap=None):
    amt = round(base * scale)
    if cap is not None:
        amt = min(amt, cap)
    return max(30, amt)


def main():
    configure_stdout_utf8()
    panels = BacktestPanels()

    ndx_base = BUY_AMOUNT_BASE_BY_CODE["NDX"]
    spx_base = BUY_AMOUNT_BASE_BY_CODE["SPX"]
    baseline_amounts = _amounts_with_us(ndx_base, spx_base, "range_4_mild")

    print(f"=== 美股限购优化 {START}~{END} ===\n")

    with _patch_config(CURRENT_US):
        ndx0 = _us_stats(panels, "ndx", ndx_base)
        spx0 = _us_stats(panels, "spx", spx_base)
    base_port = _portfolio_run(panels, baseline_amounts)

    print("基线（当前阈值 + range_4_mild）：")
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

    # 阶段1：统计各放宽档买入次数
    us_variants = {}
    for vname, overrides in RELAX_VARIANTS.items():
        with _patch_config({**CURRENT_US, **overrides}):
            us_variants[vname] = {
                "ndx": _us_stats(panels, "ndx", ndx_base),
                "spx": _us_stats(panels, "spx", spx_base),
                "overrides": {**CURRENT_US, **overrides},
            }

    # 阶段2：组合回测（放宽档 × 分档方案 × 金额缩放）
    results = []
    us_baseline = ndx0["total_bought"] + spx0["total_bought"]
    for vname, data in us_variants.items():
        ndx_s, spx_s = data["ndx"], data["spx"]
        overrides = RELAX_VARIANTS[vname]
        for tier in TIER_OPTIONS:
            for scale_name, scale in AMOUNT_SCALE_OPTIONS.items():
                ndx_amt = _scaled_amount(ndx_base, scale, cap=750)
                spx_amt = _scaled_amount(spx_base, scale, cap=180)
                amounts = _amounts_with_us(ndx_amt, spx_amt, tier)
                port = _portfolio_run(panels, amounts, overrides)
                us_invested = (
                    ndx_s["buy_count"] * ndx_amt + spx_s["buy_count"] * spx_amt
                )
                results.append({
                    "variant": vname,
                    "tier": tier,
                    "scale": scale_name,
                    "ndx_amt": ndx_amt,
                    "spx_amt": spx_amt,
                    "ndx_buys": ndx_s["buy_count"],
                    "spx_buys": spx_s["buy_count"],
                    "us_invested": us_invested,
                    "us_invest_delta": us_invested - us_baseline,
                    "port_return": port["trade_ret"],
                    "port_profit": port["trade_profit"],
                    "port_bought": port["total_bought"],
                    "profit_delta": (port["trade_profit"] or 0) - (base_port["trade_profit"] or 0),
                    "return_delta": (port["trade_ret"] or 0) - (base_port["trade_ret"] or 0),
                    "overrides": overrides,
                })

    # 筛选：利润降幅 < 2%，美股投入偏差 < 20%，买入次数增加
    extra_buys = ndx0["buy_count"] + spx0["buy_count"]
    feasible = [
        r for r in results
        if r["profit_delta"] >= -base_port["trade_profit"] * 0.02
        and abs(r["us_invest_delta"]) <= us_baseline * 0.20
        and (r["ndx_buys"] + r["spx_buys"]) >= extra_buys
        and r["ndx_amt"] <= 750
        and r["spx_amt"] <= 180
    ]
    feasible.sort(
        key=lambda r: (
            r["ndx_buys"] + r["spx_buys"],
            r["profit_delta"],
            -r["ndx_amt"],
        ),
        reverse=True,
    )

    print("--- 可行方案 Top 12（利润降幅<2%，美股投入偏差<20%，买入次数≥基线）---")
    print(
        f"{'放宽':<10} {'分档':<14} {'缩放':<6} "
        f"{'NDX次/元':>12} {'SPX次/元':>12} "
        f"{'组合利润':>10} {'Δ利润':>8} {'Δ收益':>7}"
    )
    print("-" * 90)
    for r in feasible[:12]:
        print(
            f"{r['variant']:<10} {r['tier']:<14} {r['scale']:<6} "
            f"{r['ndx_buys']:>4}/{r['ndx_amt']:<6} "
            f"{r['spx_buys']:>4}/{r['spx_amt']:<6} "
            f"{r['port_profit']:>+9,.0f} {r['profit_delta']:>+7,.0f} "
            f"{r['return_delta']:>+6.2f}pp"
        )

    best = feasible[0] if feasible else max(results, key=lambda r: r["port_profit"])
    print(f"\n推荐方案: {best['variant']} + {best['tier']} + 金额×{best['scale']}")
    print(
        f"  NDX: {best['ndx_buys']}次 × {best['ndx_amt']}元 "
        f"(基线 {ndx0['buy_count']}次 × {ndx_base}元)"
    )
    print(
        f"  SPX: {best['spx_buys']}次 × {best['spx_amt']}元 "
        f"(基线 {spx0['buy_count']}次 × {spx_base}元)"
    )
    print(
        f"  全组合利润 {best['port_profit']:+,.0f}元 "
        f"({best['profit_delta']:+,.0f}元 vs 基线 {base_port['trade_profit']:+,.0f}元)"
    )
    print(f"  全组合收益 {best['port_return']:+.2f}% ({best['return_delta']:+.2f}pp)")

    lines = [
        f"# 美股限购优化（{START} ~ {END}）",
        "",
        "> 目标：适度放宽买入标准；更细分档降低高位多买对收益的侵蚀。",
        "",
        "## 基线（当前阈值）",
        "",
        f"| 指数 | 买入次 | 单次（元） | 总投入 | 收益率 |",
        f"| --- | ---: | ---: | ---: | ---: |",
        f"| NDX | {ndx0['buy_count']} | {ndx_base} | {ndx0['total_bought']:,.0f} | {ndx0['return_pct']:+.1f}% |",
        f"| SPX | {spx0['buy_count']} | {spx_base} | {spx0['total_bought']:,.0f} | {spx0['return_pct']:+.1f}% |",
        f"| **全组合** | — | — | {base_port['total_bought']:,.0f} | "
        f"利润 **{base_port['trade_profit']:+,.0f}** / 收益 **{base_port['trade_ret']:+.2f}%** |",
        "",
        "## 推荐方案",
        "",
        f"**放宽档 `{best['variant']}`** + 分档 **`{best['tier']}`** + 基准金额缩放 **×{AMOUNT_SCALE_OPTIONS[best['scale']]:.0%}**",
        "",
        f"| 指数 | 买入次 | 单次基准（元） | 较基线 |",
        f"| --- | ---: | ---: | --- |",
        f"| NDX | {best['ndx_buys']} | {best['ndx_amt']} | "
        f"+{best['ndx_buys']-ndx0['buy_count']}次 / {best['ndx_amt']-ndx_base:+d}元 |",
        f"| SPX | {best['spx_buys']} | {best['spx_amt']} | "
        f"+{best['spx_buys']-spx0['buy_count']}次 / {best['spx_amt']-spx_base:+d}元 |",
        f"| **全组合** | — | — | 利润 **{best['port_profit']:+,.0f}** "
        f"({best['profit_delta']:+,.0f}元) / 收益 **{best['port_return']:+.2f}%** |",
        "",
        "### 分档方案",
        "",
        format_tier_table(best["tier"]),
        "",
        "### 阈值调整（相对当前 config）",
        "",
        "| 参数 | NDX 当前→推荐 | SPX 当前→推荐 |",
        "| --- | --- | --- |",
    ]
    merged_best = {**CURRENT_US, **best["overrides"]}
    for key in sorted(CURRENT_US):
        if key.startswith("NDX_"):
            spx_key = key.replace("NDX_", "SPX_")
            if spx_key in CURRENT_US:
                cv = CURRENT_US[key]
                csv = CURRENT_US[spx_key]
                nv = merged_best[key]
                nsv = merged_best[spx_key]
                if cv != nv or csv != nsv:
                    lines.append(
                        f"| {key.replace('NDX_BUY_', '')} | {cv} → {nv} | {csv} → {nsv} |"
                    )

    lines.extend(["", "### 可行方案 Top 12", ""])
    lines.append(
        "| 放宽 | 分档 | 缩放 | NDX次/元 | SPX次/元 | 组合利润 | Δ利润 | Δ收益 |"
    )
    lines.append("| --- | --- | --- | --- | --- | ---: | ---: | ---: |")
    for r in feasible[:12]:
        lines.append(
            f"| {r['variant']} | {r['tier']} | {r['scale']} | "
            f"{r['ndx_buys']}/{r['ndx_amt']} | {r['spx_buys']}/{r['spx_amt']} | "
            f"{r['port_profit']:+,.0f} | {r['profit_delta']:+,.0f} | "
            f"{r['return_delta']:+.2f}pp |"
        )

    path = BACKTEST_OUTPUT_DIR / "us_fx_quota.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"\n报告: {path}")
    return best


if __name__ == "__main__":
    main()
