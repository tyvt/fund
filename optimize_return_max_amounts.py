"""收益最大化：全指数基准金额 + 价格分档（无组合权重约束）。"""

from copy import deepcopy

from backtest_buy_signals import BacktestPanels
from backtest_trade_signals import _trade_totals, backtest_all
from buy_amount_tiers import TIER_SCHEMES, format_tier_table
from config import BACKTEST_OUTPUT_DIR, PORTFOLIO_TOTAL_BUDGET
from market_data import configure_stdout_utf8

START = "2016-01-01"
END = "2025-12-31"
TARGET_BUDGET = PORTFOLIO_TOTAL_BUDGET
BUDGET_TOLERANCE = 50_000
MIN_AMOUNT = 30

EXTRA_TIER_SCHEMES = {
    "range_5_extreme": [(0.12, 2.0), (0.24, 1.6), (0.38, 1.15), (0.52, 0.8), (1.0, 0.5)],
    "range_4_ultra": [(0.12, 2.2), (0.26, 1.55), (0.40, 1.0), (1.0, 0.55)],
}
ALL_TIER_SCHEMES = {**TIER_SCHEMES, **EXTRA_TIER_SCHEMES}


def _amounts_from_by_code(by_code):
    return {
        "dividend": 300, "cn_broad": 100, "other": 300,
        "unified": False, "portfolio": False, "by_code": dict(by_code),
    }


def _uniform_module():
    return {"dividend": 300, "cn_broad": 100, "other": 300, "unified": False, "portfolio": False, "by_code": None}


def _with_tier(amounts, scheme, normalize=True):
    out = deepcopy(amounts)
    out["tier_scheme"] = scheme
    out["tier_normalize"] = normalize
    if scheme in EXTRA_TIER_SCHEMES:
        from buy_amount_tiers import TIER_SCHEMES as TS
        TS[scheme] = EXTRA_TIER_SCHEMES[scheme]
    return out


def _collect(results):
    t = _trade_totals(results)
    return {"return_pct": t["trade_ret"], "profit": t["trade_profit"],
            "total_bought": t["total_bought"], "buy_count": t["buy_count"],
            "by_code": {r.code: r for r in results}}


def _run(amounts, panels):
    return _collect(backtest_all(START, END, amounts=amounts, panels=panels))


def _within_budget(total):
    return abs(total - TARGET_BUDGET) <= BUDGET_TOLERANCE


def _scale_to_budget(by_code, buy_counts, budget=TARGET_BUDGET):
    total = sum(buy_counts.get(c, 0) * by_code.get(c, 0) for c in buy_counts)
    if total <= 0:
        return by_code
    f = budget / total
    return {c: max(0, round(by_code.get(c, 0) * f)) for c in buy_counts}


def _index_return(r):
    return (r.return_pct if r.has_sell else r.buy_only_return_pct) or 0.0


def _return_weighted(buy_counts, returns, power=1.0, floor=MIN_AMOUNT):
    scores = {c: max(returns.get(c, 0), 0.01) ** power for c in buy_counts}
    denom = sum(buy_counts[c] * scores[c] for c in buy_counts)
    raw = {c: TARGET_BUDGET * scores[c] / denom for c in buy_counts}
    return _scale_to_budget({c: max(floor, round(v)) for c, v in raw.items()}, buy_counts)


def _top_k_focus(buy_counts, returns, k, floor=MIN_AMOUNT):
    ranked = sorted(returns.items(), key=lambda x: x[1], reverse=True)
    top = {c for c, _ in ranked[:k]}
    scores = {c: (returns[c] if c in top else 0.01) for c in buy_counts}
    denom = sum(buy_counts[c] * scores[c] for c in buy_counts)
    raw = {c: TARGET_BUDGET * scores[c] / denom for c in buy_counts}
    return _scale_to_budget({c: max(floor if c in top else 0, round(v)) for c, v in raw.items()}, buy_counts)


def _exclude_bottom(buy_counts, returns, k):
    ranked = sorted(returns.items(), key=lambda x: x[1])
    bottom = {c for c, _ in ranked[:k]}
    scores = {c: (0.0 if c in bottom else max(returns[c], 0.01)) for c in buy_counts}
    denom = sum(buy_counts[c] * scores[c] for c in buy_counts) or 1
    raw = {c: TARGET_BUDGET * scores[c] / denom for c in buy_counts}
    by_code = {c: (0 if c in bottom else max(MIN_AMOUNT, round(v))) for c, v in raw.items()}
    return _scale_to_budget(by_code, buy_counts)


def _profit_density(buy_counts, returns):
    scores = {c: max(returns[c], 0.01) * buy_counts[c] for c in buy_counts}
    denom = sum(buy_counts[c] * scores[c] for c in buy_counts)
    raw = {c: TARGET_BUDGET * scores[c] / denom for c in buy_counts}
    return _scale_to_budget({c: max(MIN_AMOUNT, round(v)) for c, v in raw.items()}, buy_counts)


def _analytical_profit(by_code, buy_counts, returns):
    """固定收益率近似：总利润 = Σ n_i × a_i × r_i。"""
    return sum(
        buy_counts[c] * by_code.get(c, 0) * returns[c] / 100
        for c in buy_counts if by_code.get(c, 0) > 0
    )


def _generate_candidates(buy_counts, returns):
    cands = {"uniform_module": None}
    cands["uniform_equal"] = {c: max(MIN_AMOUNT, round(TARGET_BUDGET / sum(buy_counts.values()))) for c in buy_counts}
    for p in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0):
        cands[f"return_p{p}"] = _return_weighted(buy_counts, returns, power=p)
    cands["profit_density"] = _profit_density(buy_counts, returns)
    for k in (3, 4, 5):
        cands[f"top{k}"] = _top_k_focus(buy_counts, returns, k)
    for k in (3, 4, 5):
        cands[f"excl_bot{k}"] = _exclude_bottom(buy_counts, returns, k)
    # 模块倾斜：美股+创业板占 58%
    groups = {
        "div": (["930955", "H30269"], 0.10),
        "broad": (["000510", "000300", "000905", "000852", "000688"], 0.28),
        "growth": (["399006", "HSTECH", "NDX", "SPX"], 0.62),
    }
    merged = {}
    for _, (codes, bw) in groups.items():
        active = [c for c in codes if c in buy_counts]
        sub = _return_weighted({c: buy_counts[c] for c in active}, {c: returns[c] for c in active}, power=2.5)
        merged.update(_scale_to_budget(sub, {c: buy_counts[c] for c in active}, TARGET_BUDGET * bw))
    cands["growth_tilt"] = _scale_to_budget(merged, buy_counts)
    return cands


def _name_map():
    from backtest_buy_signals import CN_BROAD_BACKTEST_INDICES
    from config import INDICES
    m = {i["code"]: i["name"] for i in INDICES}
    m.update({i["code"]: i["name"] for i in CN_BROAD_BACKTEST_INDICES})
    m.update({"399006": "创业板指", "HSTECH": "恒生科技", "NDX": "纳斯达克100", "SPX": "标普500"})
    return m


def _format_by_code(by_code, buy_counts):
    names = _name_map()
    lines = ["| 指数 | 代码 | 单次基准 | 买入次 | 预计投入 |", "| --- | --- | ---: | ---: | ---: |"]
    for code in sorted(by_code, key=lambda c: -by_code.get(c, 0)):
        amt = by_code.get(code, 0)
        if amt <= 0:
            continue
        n = buy_counts.get(code, 0)
        lines.append(f"| {names.get(code, code)} | {code} | {amt:.0f} | {n} | {amt * n:,.0f} |")
    inv = sum(buy_counts.get(c, 0) * by_code.get(c, 0) for c in by_code)
    lines.append(f"| **合计** | — | — | {sum(buy_counts.values())} | **{inv:,.0f}** |")
    return "\n".join(lines)


def main():
    configure_stdout_utf8()
    panels = BacktestPanels()
    print(f"=== 收益最大化 {START}~{END} ===\n")

    baseline = _run(_uniform_module(), panels)
    buy_counts = {c: r.buy_count for c, r in baseline["by_code"].items()}
    returns = {c: _index_return(r) for c, r in baseline["by_code"].items()}

    print("指数收益率：")
    for code, ret in sorted(returns.items(), key=lambda x: -x[1]):
        print(f"  {code}: {ret:+.1f}% ({buy_counts[code]}次)")

    # 解析预筛 → 只对 Top5 做完整回测
    candidates = _generate_candidates(buy_counts, returns)
    ranked = []
    for name, by_code in candidates.items():
        if by_code is None:
            ranked.append((name, None, _analytical_profit(
                {c: (300 if c in ("930955", "H30269") else 100 if c in buy_counts and c.startswith("000") else 300)
                 for c in buy_counts}, buy_counts, returns)))
            continue
        ranked.append((name, by_code, _analytical_profit(by_code, buy_counts, returns)))
    ranked.sort(key=lambda x: x[2], reverse=True)

    print("\n解析预筛 Top 8：")
    for name, _, profit in ranked[:8]:
        print(f"  {name:<22} 预估利润 {profit:+,.0f}")

    # 完整回测：基准 + 解析 Top5
    verified = [("uniform_module", None, baseline)]
    seen = {"uniform_module"}
    for name, by_code, _ in ranked:
        if name in seen:
            continue
        st = _run(_amounts_from_by_code(by_code), panels)
        if _within_budget(st["total_bought"]):
            verified.append((name, by_code, st))
            seen.add(name)
        if len(verified) >= 6:
            break

    verified.sort(key=lambda x: x[2]["return_pct"] or 0, reverse=True)
    print("\n完整回测 固定金额：")
    for name, _, st in verified:
        print(f"  {name:<22} {st['total_bought']:>9,.0f}  {st['return_pct']:>+6.2f}%  {st['profit']:>+10,.0f}")

    # 对 Top2 固定方案测试全部分档
    tier_verified = []
    for name, by_code, _ in verified[:2]:
        for tier in ALL_TIER_SCHEMES:
            st = _run(_with_tier(_amounts_from_by_code(by_code), tier, True), panels)
            if _within_budget(st["total_bought"]):
                tier_verified.append((f"{name}+{tier}", by_code, tier, st))

    # 基准 + 分档
    for tier in ALL_TIER_SCHEMES:
        st = _run(_with_tier(_uniform_module(), tier, True), panels)
        if _within_budget(st["total_bought"]):
            tier_verified.append((f"uniform_module+{tier}", None, tier, st))

    tier_verified.sort(key=lambda x: x[3]["return_pct"] or 0, reverse=True)
    print("\n完整回测 固定+分档 Top 8：")
    for name, _, _, st in tier_verified[:8]:
        print(f"  {name:<36} {st['return_pct']:>+6.2f}%  {st['profit']:>+10,.0f}")

    best_fixed = verified[0]
    best_tier = tier_verified[0] if tier_verified else None

    if best_tier and (best_tier[3]["return_pct"] or 0) > (best_fixed[2]["return_pct"] or 0):
        oname, ocode, otier, obest = best_tier[0], best_tier[1], best_tier[2], best_tier[3]
    else:
        oname, ocode, otier, obest = best_fixed[0], best_fixed[1], None, best_fixed[2]

    lines = [
        f"# 收益最大化金额配置（{START} ~ {END}）",
        "",
        "> 优化目标：全指数收益率最大化，**不使用组合权重约束**。",
        f"> 总投入 {TARGET_BUDGET:,.0f} ± {BUDGET_TOLERANCE:,.0f} 元。",
        "",
        "## 最优结果",
        "",
        f"| 项目 | 值 |",
        f"| --- | --- |",
        f"| 方案 | **{oname}** |",
        f"| 总投入 | {obest['total_bought']:,.0f} 元 |",
        f"| 收益率 | **{obest['return_pct']:+.2f}%** |",
        f"| 较基准 | **{(obest['return_pct'] or 0) - (baseline['return_pct'] or 0):+.2f}pp** |",
        f"| 分档 | {otier or '无'} |",
        "",
        "## 机制",
        "",
        "1. **基准金额**：总预算按各指数历史收益率倾斜（`amount_i ∝ return_i^p`），高收益指数（NDX、创业板、标普）获得更多单次基准。",
        "2. **单次浮动**：买入日按年区间位置分档，低位加仓、高位减投；归一化保持总投入稳定。",
        "",
    ]
    if otier:
        lines.append(format_tier_table(otier))
        lines.append("")
    if ocode:
        lines.extend(["## 推荐各指数单次基准金额", "", _format_by_code(ocode, buy_counts), ""])

    lines.extend(["## 回测对比", "", "| 方案 | 总投入 | 收益率 | 利润 | 较基准 |", "| --- | ---: | ---: | ---: | ---: |"])

    def row(lbl, st):
        d = (st["return_pct"] or 0) - (baseline["return_pct"] or 0)
        return f"| {lbl} | {st['total_bought']:,.0f} | {st['return_pct']:+.2f}% | {st['profit']:+,.0f} | {d:+.2f}pp |"

    lines.append(row("基准 300/100/300", baseline))
    for name, _, st in verified:
        lines.append(row(name, st))
    lines.extend(["", "### 分档方案", ""])
    for name, _, _, st in tier_verified[:10]:
        lines.append(row(name, st))

    if ocode:
        lines.extend(["", "## config 参考", "", "```python"])
        for code in sorted(ocode):
            if ocode.get(code, 0) > 0:
                lines.append(f'    "{code}": {ocode[code]},')
        lines.extend(["```", "", f"BUY_AMOUNT_TIER_SCHEME={otier or 'range_4_mild'}", "BUY_AMOUNT_TIER_ENABLED=true"])

    path = BACKTEST_OUTPUT_DIR / "return_max_amounts.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    print(f"\n=== 最优: {oname} ===")
    print(f"收益率 {obest['return_pct']:+.2f}% (基准 {baseline['return_pct']:+.2f}%, +{(obest['return_pct'] or 0)-(baseline['return_pct'] or 0):.2f}pp)")
    print(f"报告: {path}")


if __name__ == "__main__":
    main()
