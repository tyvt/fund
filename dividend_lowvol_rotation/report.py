# -*- coding: utf-8 -*-
"""红利低波轮动策略报告（无交易接入）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dividend_lowvol_rotation.config import (
    COMMISSION_RATE,
    DIVIDEND_YIELD_MODE,
    DYNAMIC_THRESHOLD_ENABLED,
    DYNAMIC_WEIGHT_ENABLED,
    EX_DATE_COOLDOWN_DAYS,
    EX_DATE_COOLDOWN_ENABLED,
    FUNDAMENTAL_FILTER_ENABLED,
    INDUSTRY_CAP_ENABLED,
    MAX_ANNUALIZED_VOL_PCT,
    MAX_INDUSTRY_WEIGHT,
    MAX_SINGLE_STOCK_WEIGHT,
    MIN_COMMISSION_CNY,
    MIN_DIVIDEND_YIELD_PCT,
    MIN_PROFIT_YOY_PCT,
    MIN_ROE_PCT,
    OCF_QUALITY_FILTER_ENABLED,
    PORTFOLIO_CAPITAL_CNY,
    SELL_RANK_MULTIPLIER,
    TOP_N_BUY,
    VOL_LOOKBACK_DAYS,
    VOL_RANK_WEIGHT,
    YIELD_RANK_WEIGHT,
    resolve_sell_rank,
)
from dividend_lowvol_rotation.costs import format_cost_note
from dividend_lowvol_rotation.scoring import classify_holdings
from dividend_lowvol_rotation.strategy import build_market_panel
from dividend_lowvol_rotation.symbols import parse_holdings_text
from market_data import configure_stdout_utf8


def _fmt_pct(v, digits=2):
    if v is None or (isinstance(v, float) and v != v):
        return "—"
    return f"{float(v):.{digits}f}%"


def _fmt_price(v):
    if v is None or (isinstance(v, float) and v != v):
        return "—"
    return f"{float(v):.2f}"


def _fmt_range(low, high):
    if low is None or high is None:
        return "—"
    return f"{_fmt_price(low)} ~ {_fmt_price(high)}"


def format_stock_table(df, title: str, limit: int | None = None, *, show_industry: bool = True) -> list[str]:
    lines = [f"### {title}", ""]
    if df is None or df.empty:
        lines.append("（无）")
        lines.append("")
        return lines
    show = df.head(limit) if limit else df
    if show_industry and "industry" in show.columns:
        header = "| 序号 | 代码 | 名称 | 行业 | 现价 | 股息率 | 波动 | ROE | 得分 | 挂单价区间 |"
        sep = "|------|------|------|------|------|--------|------|-----|------|------------|"
    else:
        header = "| 排名 | 代码 | 名称 | 现价 | 股息率 | 波动 | 得分 | 挂单价区间 |"
        sep = "|------|------|------|------|--------|------|------|------------|"
    lines.append(header)
    lines.append(sep)
    for i, (_, r) in enumerate(show.iterrows(), start=1):
        rank = int(r.get("portfolio_rank") or r.get("rank") or i)
        roe = _fmt_pct(r.get("roe_pct")) if "roe_pct" in r else "—"
        if show_industry and "industry" in show.columns:
            ind = str(r.get("industry", ""))[:10]
            lines.append(
                "| {seq} | {code} | {name} | {ind} | {price} | {dy} | {vol} | {roe} | {score:.1f} | {brange} |".format(
                    seq=rank,
                    code=r["code"],
                    name=str(r["name"])[:8],
                    ind=ind,
                    price=_fmt_price(r["price"]),
                    dy=_fmt_pct(r["dividend_yield_pct"]),
                    vol=_fmt_pct(r["ann_vol_pct"]),
                    roe=roe,
                    score=float(r["composite_score"]),
                    brange=_fmt_range(r.get("buy_low"), r.get("buy_high")),
                )
            )
        else:
            lines.append(
                "| {rank} | {code} | {name} | {price} | {dy} | {vol} | {score:.1f} | {brange} |".format(
                    rank=rank,
                    code=r["code"],
                    name=str(r["name"])[:8],
                    price=_fmt_price(r["price"]),
                    dy=_fmt_pct(r["dividend_yield_pct"]),
                    vol=_fmt_pct(r["ann_vol_pct"]),
                    score=float(r["composite_score"]),
                    brange=_fmt_range(r.get("buy_low"), r.get("buy_high")),
                )
            )
    lines.append("")
    return lines


def build_report(
    ranked,
    buy_pool,
    meta,
    holdings: list[str] | None = None,
    *,
    top_n: int = TOP_N_BUY,
    sell_rank: int | None = None,
    capital_cny: float | None = None,
) -> str:
    sell_rank = resolve_sell_rank(top_n, sell_rank)
    lines = [
        "## A 股红利低波轮动（EasyXT 逻辑 · 风控增强 · 仅评估）",
        "",
        f"**生成时间**：{meta.get('as_of', '—')}  ",
        f"**耗时**：{meta.get('elapsed_sec', '—')} 秒",
        "",
        "### 策略参数",
        "",
        f"- 股息率模式：**{DIVIDEND_YIELD_MODE}**（latest/ttm/auto）",
        f"- 年化波动：{VOL_LOOKBACK_DAYS} 日；静态上限 {MAX_ANNUALIZED_VOL_PCT:.0f}%",
        f"- 排名权重：股息率 {YIELD_RANK_WEIGHT:g} + 低波 {VOL_RANK_WEIGHT:g}"
        + ("（动态）" if DYNAMIC_WEIGHT_ENABLED else ""),
        f"- **买入 {top_n} 只**；**跌出前 {sell_rank} 名**（{SELL_RANK_MULTIPLIER}× 缓冲）",
        "",
    ]
    dyn = meta.get("dynamic") or {}
    if dyn or DYNAMIC_THRESHOLD_ENABLED:
        lines.extend(["### 动态参数（当日）", ""])
        if dyn:
            lines.append(f"- 股息率门槛：**{dyn.get('min_yield_pct', MIN_DIVIDEND_YIELD_PCT):.2f}%**")
            lines.append(f"- 波动上限：**{dyn.get('max_vol_pct', MAX_ANNUALIZED_VOL_PCT):.1f}%**")
            if dyn.get("bond_yield_pct") is not None:
                lines.append(f"- 10Y 国债：**{dyn['bond_yield_pct']:.2f}%**")
            if dyn.get("market_vol_median_pct") is not None:
                lines.append(f"- 波动中位：**{dyn['market_vol_median_pct']:.1f}%**")
            for note in dyn.get("notes", []):
                lines.append(f"- {note}")
            lines.append(
                f"- 权重：股息 **{dyn.get('yield_weight', YIELD_RANK_WEIGHT):.2f}** / "
                f"低波 **{dyn.get('vol_weight', VOL_RANK_WEIGHT):.2f}**"
            )
        lines.append("")
    mkt_val = meta.get("market_valuation") or {}
    if mkt_val.get("market_pe_percentile") is not None:
        lines.extend(["### 全市场估值锚点", ""])
        lines.append(
            f"- 中证800 PE：**{mkt_val.get('market_pe', 0):.2f}**，"
            f"历史分位 **{mkt_val['market_pe_percentile']:.1f}%**"
        )
        if mkt_val.get("valuation_tight"):
            lines.append("- 状态：**收紧买入**（PB 偏好降至行业 30% 分位）")
        if mkt_val.get("pause_new_buys"):
            lines.append("- 状态：**暂停新增买入**")
        lines.append("")
    risk_ind = meta.get("risk_pass_by_industry") or []
    if risk_ind:
        lines.extend(["### 排雷通过率（按行业）", ""])
        low = sorted(risk_ind, key=lambda r: r.get("pass_rate_pct", 100))[:8]
        for row in low:
            lines.append(
                f"- {row['industry']}：{row['passed']}/{row['total']} "
                f"（**{row['pass_rate_pct']:.0f}%**）"
            )
        lines.append("")
    lines.extend(
        [
        "### 风控过滤",
        "",
    ])
    if EX_DATE_COOLDOWN_ENABLED:
        lines.append(f"- 除权冷却：除权后 {EX_DATE_COOLDOWN_DAYS} 日内剔除")
    else:
        lines.append("- 除权冷却：关")
    if FUNDAMENTAL_FILTER_ENABLED:
        lines.append(f"- 基本面：ROE ≥ {MIN_ROE_PCT:g}%，净利润同比 ≥ {MIN_PROFIT_YOY_PCT:g}%")
    else:
        lines.append("- 基本面过滤：关")
    if INDUSTRY_CAP_ENABLED:
        lines.append(
            f"- 行业分散：单行业 ≤ {MAX_INDUSTRY_WEIGHT * 100:.0f}%；"
            f"单股 ≤ {MAX_SINGLE_STOCK_WEIGHT * 100:.0f}%"
        )
    else:
        lines.append("- 行业分散：关")
    if OCF_QUALITY_FILTER_ENABLED:
        lines.append("- 现金流质量：开（经营现金流/净利润）")
    lines.append("")

    cost_note = format_cost_note(capital_cny, top_n)
    if cost_note:
        lines.append("### 交易成本估算")
        lines.append("")
        lines.append(
            f"- 佣金：万 {COMMISSION_RATE * 10000:.3f}，单边最低 {MIN_COMMISSION_CNY:.0f} 元"
        )
        lines.append(f"- {cost_note}")
        lines.append("")

    lines.append("### 数据步骤")
    lines.append("")
    for step in meta.get("steps", []):
        lines.append(f"- {step}")
    lines.append("")
    if meta.get("warnings"):
        lines.append("### 注意")
        lines.append("")
        for w in meta["warnings"]:
            lines.append(f"- ⚠ {w}")
        lines.append("")

    if ranked is None or ranked.empty:
        lines.append("**未筛出符合条件的股票**，请检查网络或放宽参数。")
        lines.append("")
        return "\n".join(lines)

    lines.extend(
        format_stock_table(
            buy_pool if buy_pool is not None and not buy_pool.empty else ranked.head(top_n),
            f"建议买入（{top_n} 只，含行业约束）",
            limit=top_n,
        )
    )

    lines.append("### 买入价区间说明")
    lines.append("")
    lines.append(
        "区间 = [近60日低点, min(现价×99%, 股息率门槛价, 近60日低点+3%)]；仅供条件单参考。"
    )
    lines.append("")

    if holdings:
        cls = classify_holdings(
            ranked, holdings, top_n=top_n, sell_rank=sell_rank, buy_pool=buy_pool
        )
        lines.append("### 相对持仓")
        lines.append("")
        lines.append(f"- **建议新增买入**：{', '.join(cls['buy_new']) or '无'}")
        lines.append(f"- **继续持有**（排名≤{sell_rank}）：{', '.join(cls['hold_ok']) or '无'}")
        lines.append(f"- **关注卖出**（排名>{sell_rank}）：{', '.join(cls['sell_watch']) or '无'}")
        if cls["not_in_pool"]:
            lines.append(f"- **未在候选池**：{', '.join(cls['not_in_pool'])}")
        lines.append("")

    lines.append("### 候选池统计")
    lines.append("")
    lines.append(f"- 全量排名：**{len(ranked)}** 只")
    lines.append(
        f"- 股息率中位 {_fmt_pct(ranked['dividend_yield_pct'].median())}，"
        f"波动中位 {_fmt_pct(ranked['ann_vol_pct'].median())}"
    )
    if "industry" in ranked.columns:
        top_ind = ranked["industry"].value_counts().head(5)
        ind_text = "；".join(f"{k} {v}只" for k, v in top_ind.items())
        lines.append(f"- 行业分布（前5）：{ind_text}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="A股红利低波轮动评估（无交易）")
    parser.add_argument("--refresh", action="store_true", help="忽略今日缓存，重新拉取")
    parser.add_argument(
        "--top",
        type=int,
        default=TOP_N_BUY,
        help=f"买入只数（默认 {TOP_N_BUY}；小资金可设 3）",
    )
    parser.add_argument(
        "--sell-rank",
        type=int,
        default=None,
        help=f"跌出该排名关注卖出；默认 top×{SELL_RANK_MULTIPLIER}（如 top=3 → 6）",
    )
    parser.add_argument("--holdings", type=str, help="持仓文件，每行一个代码")
    parser.add_argument(
        "--capital",
        type=float,
        default=None,
        help=f"资金量（元），用于佣金估算；也可用环境变量 DLV_PORTFOLIO_CAPITAL_CNY（当前 {PORTFOLIO_CAPITAL_CNY:g}）",
    )
    parser.add_argument("-o", "--output", type=str, help="另存 Markdown")
    args = parser.parse_args(argv)

    holdings: list[str] | None = None
    if args.holdings:
        path = Path(args.holdings)
        if not path.exists():
            print(f"持仓文件不存在: {path}", file=sys.stderr)
            return 1
        holdings = parse_holdings_text(path.read_text(encoding="utf-8"))

    sell_rank = resolve_sell_rank(args.top, args.sell_rank)
    print(
        f"正在拉取数据（买入 {args.top} 只，缓冲前 {sell_rank} 名）…"
    )
    ranked, buy_pool, meta = build_market_panel(
        refresh=args.refresh, top_n=args.top, sell_rank=sell_rank
    )
    report = build_report(
        ranked,
        buy_pool,
        meta,
        holdings=holdings,
        top_n=args.top,
        sell_rank=sell_rank,
        capital_cny=args.capital,
    )
    print(report)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
