"""优化各指数基准金额 + 价格分档系数（总投入约束 ±5 万）。"""

from copy import deepcopy
from pathlib import Path

from backtest_buy_signals import (
    BacktestPanels,
    CN_BROAD_BACKTEST_INDICES,
    US_INDEX_META,
    US_INDEX_KEYS,
    _us_buy_snapshot,
)
from backtest_trade_signals import (
    _cn_broad_signals,
    _cyb_signals,
    _hstech_signals,
    _trade_totals,
    backtest_all,
)
from buy_amount_tiers import TIER_SCHEMES, format_tier_table, get_tier_scheme
from config import (
    BACKTEST_OUTPUT_DIR,
    CYB_INDEX,
    HSTECH_INDEX,
    INDICES,
    PORTFOLIO_INDEX_GROUPS,
    PORTFOLIO_TOTAL_BUDGET,
    _BACKTEST_BUY_AMOUNT_DEFAULTS,
    resolve_backtest_amounts,
)
from dividend_data import is_buy_signal_row
from market_data import configure_stdout_utf8

START = "2016-01-01"
END = "2025-12-31"
BUDGET_TOLERANCE = 50_000


def _uniform_amounts():
    return {
        "dividend": 300,
        "cn_broad": 100,
        "other": 300,
        "unified": False,
        "portfolio": False,
        "by_code": None,
    }


def _portfolio_amounts(by_code=None):
    base = resolve_backtest_amounts(portfolio_mode=True)
    if by_code:
        base["by_code"] = dict(by_code)
    return base


def _with_tier(amounts, scheme, normalize=True):
    out = deepcopy(amounts)
    out["tier_scheme"] = scheme
    out["tier_normalize"] = normalize
    return out


def _collect(results):
    totals = _trade_totals(results)
    return {
        "return_pct": totals["trade_ret"],
        "profit": totals["trade_profit"],
        "total_bought": totals["total_bought"],
        "buy_count": totals["buy_count"],
        "by_code": {r.code: r for r in results},
    }


def _run(amounts, panels):
    return _collect(backtest_all(START, END, amounts=amounts, panels=panels))


def _index_panels_and_buy_fns(panels):
    """返回 (code, panel, buy_fn, date_col) 列表，用于估算分档归一化系数。"""
    items = []
    for item in INDICES:
        code = item["code"]
        panel = panels.dividend_panel(code)
        items.append((code, panel, lambda r, c=code: is_buy_signal_row(r, c), "date"))
    for item in CN_BROAD_BACKTEST_INDICES:
        code = item["code"]
        panel = panels.cn_broad_panel(code)
        items.append(
            (code, panel, lambda r, c=code: _cn_broad_signals(r, c)[0], "date")
        )
    cyb_panel = panels.cyb_panel()
    items.append(
        (CYB_INDEX["code"], cyb_panel, lambda r: _cyb_signals(r)[0], "date")
    )
    hs_panel = panels.hstech_panel()
    items.append(
        (HSTECH_INDEX["code"], hs_panel, lambda r: _hstech_signals(r)[0], "date")
    )
    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        meta = US_INDEX_META[key]
        items.append(
            (
                meta["code"],
                daily,
                lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g),
                "date",
            )
        )
    return items


def _build_tier_norm_by_code(base_amounts, scheme, panels):
    from buy_amount_tiers import estimate_avg_multiplier

    by_code = base_amounts.get("by_code") or {}
    norm = {}
    for code, panel, buy_fn, date_col in _index_panels_and_buy_fns(panels):
        base = by_code.get(code)
        if base is None or base <= 0:
            continue
        avg = estimate_avg_multiplier(
            panel, START, END, buy_fn, scheme, date_col=date_col
        )
        norm[code] = 1.0 / avg if avg > 0 else 1.0
    return norm


def _scale_portfolio_to_budget(by_code, buy_counts, budget):
    total = sum(buy_counts.get(c, 0) * by_code.get(c, 0) for c in by_code)
    if total <= 0:
        return by_code
    factor = budget / total
    return {c: round(v * factor) for c, v in by_code.items()}


def _optimize_base_tilt(portfolio_buys, portfolio_returns, budget):
    """在组合权重框架下微调组内倾斜（复用 optimize_portfolio_amounts 思路）。"""
    from optimize_portfolio_amounts import _grid_us_satellite, _scale_to_budget

    best_code = None
    best_ret = -1.0
    for us_share in (0.75, 0.80, 0.85, 0.90):
        for sat_share in (0.70, 0.75, 0.80, 0.85):
            code = _grid_us_satellite(
                portfolio_buys, portfolio_returns, us_share, sat_share
            )
            code = _scale_to_budget(code, portfolio_buys, budget)
            yield code


def _within_budget(total_bought, target, tol=BUDGET_TOLERANCE):
    return abs(total_bought - target) <= tol


def _format_amounts_table(by_code, buy_counts):
    lines = ["| 指数 | 代码 | 单次基准 | 买入次 | 预计投入 |", "| --- | --- | ---: | ---: | ---: |"]
    name_map = {i["code"]: i["name"] for i in INDICES}
    name_map.update({i["code"]: i["name"] for i in CN_BROAD_BACKTEST_INDICES})
    name_map.update({CYB_INDEX["code"]: CYB_INDEX["name"]})
    name_map.update({"NDX": "纳斯达克100", "SPX": "标普500"})
    for code in sorted(by_code.keys()):
        amt = by_code.get(code, 0)
        if amt <= 0 and code not in PORTFOLIO_INDEX_GROUPS:
            continue
        n = buy_counts.get(code, 0)
        lines.append(
            f"| {name_map.get(code, code)} | {code} | {amt:.0f} | {n} | {amt * n:,.0f} |"
        )
    return "\n".join(lines)


def save_report(content: str, filename="tiered_amounts_optimization.md"):
    path = BACKTEST_OUTPUT_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def main():
    configure_stdout_utf8()
    panels = BacktestPanels()
    target_budget = PORTFOLIO_TOTAL_BUDGET

    # 基准：全指数统一金额（316,200）
    base_uniform = _run(_uniform_amounts(), panels)
    buy_counts = {c: r.buy_count for c, r in base_uniform["by_code"].items()}
    returns = {
        c: (r.return_pct if r.has_sell else r.buy_only_return_pct) or 0.0
        for c, r in base_uniform["by_code"].items()
    }
    portfolio_buys = {c: buy_counts[c] for c in PORTFOLIO_INDEX_GROUPS if c in buy_counts}
    portfolio_returns = {c: returns[c] for c in PORTFOLIO_INDEX_GROUPS if c in returns}

    # 组合固定基准金额
    portfolio_fixed = _portfolio_amounts()
    port_fixed_stats = _run(portfolio_fixed, panels)

    # 组合基准金额网格微调
    best_base = dict(_BACKTEST_BUY_AMOUNT_DEFAULTS)
    best_base_stats = port_fixed_stats
    for tilted in _optimize_base_tilt(portfolio_buys, portfolio_returns, target_budget):
        amt = _portfolio_amounts(tilted)
        st = _run(amt, panels)
        if st["return_pct"] and st["return_pct"] > (best_base_stats["return_pct"] or 0):
            best_base_stats = st
            best_base = tilted

    best_base_amounts = _portfolio_amounts(best_base)

    # 分档方案对比（在最优基准上）
    tier_results = []
    for scheme_name in TIER_SCHEMES:
        tier_amt = _with_tier(best_base_amounts, scheme_name, normalize=True)
        st = _run(tier_amt, panels)
        tier_results.append((scheme_name, st))

    tier_results.sort(key=lambda x: x[1]["return_pct"] or 0, reverse=True)
    best_tier_name, best_tier_stats = tier_results[0]

    # 全指数统一金额 + 分档（不排除任何指数）
    uniform_tier_results = []
    for scheme_name in TIER_SCHEMES:
        tier_amt = _with_tier(_uniform_amounts(), scheme_name, normalize=True)
        st = _run(tier_amt, panels)
        uniform_tier_results.append((scheme_name, st))
    uniform_tier_results.sort(key=lambda x: x[1]["return_pct"] or 0, reverse=True)
    best_uniform_tier_name, best_uniform_tier_stats = uniform_tier_results[0]

    # 输出
    lines = [
        f"# 基准金额 + 价格分档优化（{START} ~ {END}）",
        "",
        f"> 总投入目标 **{target_budget:,.0f}** 元，允许偏差 ±**{BUDGET_TOLERANCE:,.0f}** 元",
        "",
        "## 机制设计",
        "",
        "### 1. 各指数单次投入基准金额",
        "",
        "在 **总预算不变** 前提下，按四组权重分配，组内按历史收益率倾斜：",
        "",
        "| 组别 | 权重 | 分配原则 |",
        "| --- | ---: | --- |",
        "| 核心（红利+A500） | 50% | 组内按收益率加权 |",
        "| 美股（NDX+SPX） | 20% | 偏 NDX（约 85%） |",
        "| 科创50 | 10% | 单指数 |",
        "| 卫星（创业板+1000） | 20% | 偏创业板（约 80%） |",
        "",
        "沪深300、中证500、恒生科技 **不买入**（组合外）。",
        "",
        "公式：`单次基准_i = 组预算 × r_i / Σ(n_j × r_j)`，其中 `r_i` 为指数历史收益率，`n_j` 为买入次数。",
        "",
        "### 2. 按实时买入价格分档",
        "",
        "每次触发买入信号时，根据当日 **年区间位置**（0=近年内低点，1=近年内高点）调整实际投入：",
        "",
        f"实际投入 = 单次基准 × 分档系数",
        "",
        format_tier_table(best_tier_name),
        "",
        "归一化：各指数按历史买入日的平均分档系数缩放基准，使 **总分投入接近固定金额方案**。",
        "",
        "## 回测对比",
        "",
        "| 方案 | 总投入 | 总利润 | 收益率 | 较基准 | 投入偏差 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]

    def _row(name, st, baseline_ret):
        delta = (st["return_pct"] or 0) - (baseline_ret or 0)
        dev = st["total_bought"] - target_budget
        ok = "✓" if _within_budget(st["total_bought"], target_budget) else "✗"
        return (
            f"| {name} | {st['total_bought']:,.0f} | {st['profit']:+,.0f} | "
            f"{st['return_pct']:+.1f}% | {delta:+.1f}pp | {dev:+,.0f} {ok} |"
        )

    baseline_ret = base_uniform["return_pct"]
    lines.append(_row("A. 全指数统一金额（300/100/300）", base_uniform, baseline_ret))
    lines.append(_row("B. 组合固定基准金额", port_fixed_stats, baseline_ret))
    lines.append(_row("C. 组合优化基准金额", best_base_stats, baseline_ret))
    for name, st in tier_results:
        lines.append(_row(f"D. 优化基准 + {name}", st, baseline_ret))

    lines.extend([
        "",
        "### 全指数统一金额 + 分档（含沪深300/中证500/恒科）",
        "",
        "| 方案 | 总投入 | 总利润 | 收益率 | 较基准 | 投入偏差 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    for name, st in uniform_tier_results:
        lines.append(_row(f"E. 统一金额 + {name}", st, baseline_ret))

    lines.extend([
        "",
        f"**推荐方案（组合模式）**：C + **{best_tier_name}** → 收益率 **{best_tier_stats['return_pct']:+.1f}%**",
        f"（总投入 {best_tier_stats['total_bought']:,.0f} 元，利润 {best_tier_stats['profit']:+,.0f} 元）",
        "",
        f"**推荐方案（全指数）**：A + **{best_uniform_tier_name}** → 收益率 **{best_uniform_tier_stats['return_pct']:+.1f}%**",
        f"（总投入 {best_uniform_tier_stats['total_bought']:,.0f} 元，较固定金额 +{(best_uniform_tier_stats['return_pct'] or 0) - (baseline_ret or 0):.2f}pp）",
        "",
        "## 推荐各指数单次基准金额（元）",
        "",
        _format_amounts_table(best_base, buy_counts),
        "",
        "## 较统一金额方案的增量贡献（推荐分档方案）",
        "",
        "| 指数 | 代码 | 固定投入 | 分档投入 | 固定利润 | 分档利润 | 增量 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ])

    fixed_tier = _run(_with_tier(best_base_amounts, best_tier_name, True), panels)
    for code in sorted(best_base.keys()):
        if best_base.get(code, 0) <= 0:
            continue
        r_fix = best_base_stats["by_code"].get(code)
        r_tier = fixed_tier["by_code"].get(code)
        if not r_fix or not r_tier:
            continue
        p_fix = r_fix.profit if r_fix.has_sell else r_fix.buy_only_profit
        p_tier = r_tier.profit if r_tier.has_sell else r_tier.buy_only_profit
        lines.append(
            f"| {name_map.get(code, code)} | {code} | {r_fix.total_bought:,.0f} | {r_tier.total_bought:,.0f} | "
            f"{p_fix:+,.0f} | {p_tier:+,.0f} | {p_tier - p_fix:+,.0f} |"
        )

    lines.extend([
        "",
        "## 实施要点",
        "",
        "1. **基准金额**写入 `config.py` 的 `_BACKTEST_BUY_AMOUNT_DEFAULTS`，通过 `--portfolio` 回测/实盘参考。",
        "2. **分档系数**在 `buy_amount_tiers.py` 中维护；买入时读取当日 `year_range_position` 或 `pct_above_low`。",
        "3. 归一化缩放保证总投入在目标 ±5 万以内；若实际买入日与回测分布不同，可每季度重算 `tier_norm_by_code`。",
        "4. 低位加仓、高位减投的核心逻辑：**同样总资金，把更多份额买在更便宜的日子**。",
        "",
        "## 分档方案明细",
        "",
    ])
    for scheme_name in TIER_SCHEMES:
        lines.append(format_tier_table(scheme_name))
        lines.append("")

    report = "\n".join(lines).rstrip() + "\n"
    path = save_report(report)
    print(report)
    print(f"\n报告已保存: {path}")


if __name__ == "__main__":
    main()
