# -*- coding: utf-8 -*-
"""红利低波轮动策略报告（无交易接入）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dividend_lowvol_rotation.config import (
    BACKTEST_INITIAL_CAPITAL,
    BACKTEST_PREFETCH_SIZE,
    COMMISSION_RATE,
    DIVIDEND_YIELD_MODE,
    DYNAMIC_WEIGHT_ENABLED,
    INDEX_STYLE_RANKING,
    LIVE_REBALANCE_MODE,
    INDUSTRY_CAP_ENABLED,
    LOT_SIZE,
    MAX_ANNUALIZED_VOL_PCT,
    MAX_INDUSTRY_WEIGHT,
    MAX_SINGLE_STOCK_WEIGHT,
    MIN_COMMISSION_CNY,
    MIN_DIVIDEND_YIELD_PCT,
    RECENT_DIVIDEND_HARD_FILTER_ENABLED,
    RECENT_DIVIDEND_MAX_YEARS,
    PORTFOLIO_CAPITAL_CNY,
    SELL_MODE,
    SELL_RANK_MULTIPLIER,
    SOFT_RISK_SCORING_ENABLED,
    TOP_N_BUY,
    VOL_LOOKBACK_DAYS,
    VOL_RANK_WEIGHT,
    YIELD_RANK_WEIGHT,
    resolve_sell_rank,
)
from dividend_lowvol_rotation.costs import (
    format_cost_note,
    plan_portfolio_allocation,
    resolve_report_capital,
)
from dividend_lowvol_rotation.index_portfolio import classify_index_portfolio
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


def _fmt_money(v):
    if v is None or (isinstance(v, float) and v != v):
        return "—"
    return f"{float(v):,.0f}"


def _fmt_shares(v):
    if v is None or (isinstance(v, float) and v != v):
        return "—"
    return f"{int(v):,}"


def _fmt_rebalance_schedule(meta: dict) -> str:
    mode = str(meta.get("rebalance_mode") or LIVE_REBALANCE_MODE).lower()
    if mode == "entry_anniversary":
        entry = meta.get("entry_date", "—")
        nxt = meta.get("next_rebalance_date", "—")
        return f"**建仓周年**（建仓日 {entry}，下次调仓 {nxt} 后首个交易日）"
    if mode == "index_annual":
        from dividend_lowvol_rotation.config import INDEX_ANNUAL_REBALANCE_TIMING

        timing = "1 月中旬" if INDEX_ANNUAL_REBALANCE_TIMING == "january" else "12 月指数调样"
        return f"**{timing}** 年度调样"
    return f"**{mode}**"


def format_stock_table(
    df,
    title: str,
    limit: int | None = None,
    *,
    show_industry: bool = True,
    show_weight: bool = False,
    show_allocation: bool = False,
) -> list[str]:
    lines = [f"### {title}", ""]
    if df is None or df.empty:
        lines.append("（无）")
        lines.append("")
        return lines
    show = df.head(limit) if limit else df
    weight_col = show_weight and "target_weight_pct" in show.columns
    alloc_col = show_allocation and "buy_shares" in show.columns
    if show_industry and "industry" in show.columns:
        header = "| 序号 | 代码 | 名称 | 行业 | 现价 | 股息率 | 波动 | ROE | 得分 |"
        if weight_col:
            header += " 目标权重 |"
        if alloc_col:
            header += " 买入股数 | 预估金额 |"
        header += " 挂单价区间 |"
        sep = "|------|------|------|------|------|--------|------|-----|------|"
        if weight_col:
            sep += "----------|"
        if alloc_col:
            sep += "----------|----------|"
        sep += "------------|"
    else:
        header = "| 排名 | 代码 | 名称 | 现价 | 股息率 | 波动 | 得分 |"
        if alloc_col:
            header += " 买入股数 | 预估金额 |"
        header += " 挂单价区间 |"
        sep = "|------|------|------|------|--------|------|------|"
        if alloc_col:
            sep += "----------|----------|"
        sep += "------------|"
    lines.append(header)
    lines.append(sep)
    for i, (_, r) in enumerate(show.iterrows(), start=1):
        rank = int(r.get("portfolio_rank") or r.get("rank") or i)
        roe = _fmt_pct(r.get("roe_pct")) if "roe_pct" in r else "—"
        weight_cell = (
            f" {_fmt_pct(r.get('target_weight_pct'))} |"
            if weight_col and r.get("target_weight_pct") is not None
            else (" — |" if weight_col else "")
        )
        alloc_cell = ""
        if alloc_col:
            shares = int(r.get("buy_shares") or 0)
            amount = r.get("buy_amount_cny")
            alloc_cell = (
                f" {_fmt_shares(shares)} | {_fmt_money(amount)} |"
                if shares > 0
                else " — | — |"
            )
        if show_industry and "industry" in show.columns:
            ind = str(r.get("industry", ""))[:10]
            lines.append(
                "| {seq} | {code} | {name} | {ind} | {price} | {dy} | {vol} | {roe} | {score:.1f} |{weight}{alloc}{brange} |".format(
                    seq=rank,
                    code=r["code"],
                    name=str(r["name"])[:8],
                    ind=ind,
                    price=_fmt_price(r["price"]),
                    dy=_fmt_pct(r["dividend_yield_pct"]),
                    vol=_fmt_pct(r["ann_vol_pct"]),
                    roe=roe,
                    score=float(r.get("composite_score") or rank),
                    weight=weight_cell,
                    alloc=alloc_cell,
                    brange=f" {_fmt_range(r.get('buy_low'), r.get('buy_high'))}",
                )
            )
        else:
            lines.append(
                "| {rank} | {code} | {name} | {price} | {dy} | {vol} | {score:.1f} |{alloc}{brange} |".format(
                    rank=rank,
                    code=r["code"],
                    name=str(r["name"])[:8],
                    price=_fmt_price(r["price"]),
                    dy=_fmt_pct(r["dividend_yield_pct"]),
                    vol=_fmt_pct(r["ann_vol_pct"]),
                    score=float(r.get("composite_score") or rank),
                    alloc=alloc_cell,
                    brange=f" {_fmt_range(r.get('buy_low'), r.get('buy_high'))}",
                )
            )
    lines.append("")
    return lines


def build_report(
    ranked,
    target_portfolio,
    meta,
    holdings: list[str] | None = None,
    *,
    top_n: int = TOP_N_BUY,
    sell_rank: int | None = None,
    capital_cny: float | None = None,
) -> str:
    sell_rank = resolve_sell_rank(top_n, sell_rank)
    effective_top_n = int(meta.get("effective_top_n") or top_n)
    prefetch_size = int(meta.get("prefetch_size") or BACKTEST_PREFETCH_SIZE)
    sell_mode = str(meta.get("sell_mode") or SELL_MODE).lower()
    effective_capital = resolve_report_capital(capital_cny)

    lines = [
        "# A 股红利低波轮动评估报告",
        "",
        "## A 股红利低波轮动（实盘评估 · 与回测同逻辑）",
        "",
        f"**生成时间**：{meta.get('as_of', '—')}  ",
        f"**耗时**：{meta.get('elapsed_sec', '—')} 秒",
        "",
        "### 资金与建仓",
        "",
        f"- **初始资金**：**{_fmt_money(effective_capital)}** 元"
        + (
            f"（`--capital` / 环境变量 `DLV_PORTFOLIO_CAPITAL_CNY`；未设则默认回测初始 {BACKTEST_INITIAL_CAPITAL:,.0f} 元）"
            if capital_cny is None and PORTFOLIO_CAPITAL_CNY <= 0
            else ""
        ),
        f"- **最小交易单位**：{LOT_SIZE} 股/手；买入股数按目标权重向下取整手，含单边佣金",
        "",
        "### 策略参数",
        "",
        f"- 候选预筛：与回测相同，Top **{prefetch_size}**（`fhps_yield_pct` 排序）",
        f"- 股息率模式：**{DIVIDEND_YIELD_MODE}**（latest/ttm/auto；auto 无 TTM 不回退旧分红）",
        f"- 近 **{RECENT_DIVIDEND_MAX_YEARS}** 年分红硬过滤：**{'开' if RECENT_DIVIDEND_HARD_FILTER_ENABLED else '关'}**",
        f"- 年化波动：{VOL_LOOKBACK_DAYS} 日；静态上限 {MAX_ANNUALIZED_VOL_PCT:.0f}%",
        f"- 排序：**{'股息率→低波（指数式）' if INDEX_STYLE_RANKING else f'加权 {YIELD_RANK_WEIGHT:g}+{VOL_RANK_WEIGHT:g}'}**",
        f"- 调仓：{_fmt_rebalance_schedule(meta)}；调出模式 **{sell_mode}**",
        f"- 目标持仓 **{effective_top_n}** 只"
        + (f"（配置 {top_n}，仓位缩放 {meta.get('position_scale', 1) * 100:.0f}%）" if effective_top_n != top_n else ""),
        "",
    ]
    if sell_mode != "index_rules":
        lines.append(
            f"- **跌出前 {sell_rank} 名**关注卖出（{SELL_RANK_MULTIPLIER}× 缓冲）"
        )
        lines.append("")

    dyn = meta.get("dynamic") or {}
    if dyn:
        lines.extend(["### 动态参数（当日）", ""])
        lines.append(f"- 股息率门槛：**{dyn.get('min_yield_pct', MIN_DIVIDEND_YIELD_PCT):.2f}%**")
        lines.append(f"- 波动上限：**{dyn.get('max_vol_pct', MAX_ANNUALIZED_VOL_PCT):.1f}%**")
        if dyn.get("bond_yield_pct") is not None:
            lines.append(f"- 10Y 国债：**{dyn['bond_yield_pct']:.2f}%**")
        if dyn.get("market_vol_median_pct") is not None:
            lines.append(f"- 波动中位：**{dyn['market_vol_median_pct']:.1f}%**")
        for note in dyn.get("notes", []):
            lines.append(f"- {note}")
        if DYNAMIC_WEIGHT_ENABLED:
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
        lines.extend(["### 排雷通过率（按行业，软评分模式下仅供参考）", ""])
        low = sorted(risk_ind, key=lambda r: r.get("pass_rate_pct", 100))[:8]
        for row in low:
            lines.append(
                f"- {row['industry']}：{row['passed']}/{row['total']} "
                f"（**{row['pass_rate_pct']:.0f}%**）"
            )
        lines.append("")

    risk_mode = "软评分（与回测一致）" if SOFT_RISK_SCORING_ENABLED else "硬过滤"
    lines.extend(
        [
            "### 风控过滤",
            "",
            f"- 排雷：**{risk_mode}**（ROE 波动 / 分红年数 / 支付率 / 负债率 / 利息保障）",
        ]
    )
    if INDUSTRY_CAP_ENABLED:
        lines.append(
            f"- 行业分散：单行业 ≤ {MAX_INDUSTRY_WEIGHT * 100:.0f}%；"
            f"单股 ≤ {MAX_SINGLE_STOCK_WEIGHT * 100:.0f}%"
        )
    else:
        lines.append("- 行业分散：关")
    lines.append("")

    cost_note = format_cost_note(effective_capital, top_n)
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

    show_df = (
        target_portfolio
        if target_portfolio is not None and not target_portfolio.empty
        else ranked.head(effective_top_n)
    )
    show_df, alloc_summary = plan_portfolio_allocation(show_df, effective_capital)
    lines.extend(
        [
            "### 建仓汇总",
            "",
            f"- **预估总投入**：{_fmt_money(alloc_summary.get('total_invested_cny'))} 元"
            f"（含佣金 {_fmt_money(alloc_summary.get('total_commission_cny'))} 元）",
            f"- **剩余现金**：{_fmt_money(alloc_summary.get('cash_remaining_cny'))} 元"
            f"（资金利用率 {_fmt_pct(alloc_summary.get('utilization_pct'))}）",
            "",
        ]
    )
    lines.extend(
        format_stock_table(
            show_df,
            f"目标组合（{len(meta.get('target_codes') or [])} 只，股息率加权）",
            limit=effective_top_n,
            show_weight=True,
            show_allocation=True,
        )
    )

    lines.append("### 买入价区间说明")
    lines.append("")
    lines.append(
        "区间 = [现价×(1−下沿%), min(现价×(1+上沿%), 股息率门槛价)]；默认下沿 1%、上沿 0%。"
    )
    lines.append(
        "回测在调仓日按收盘价+滑点成交；今日建仓即视为首次调仓，次年建仓周年再调仓。"
    )
    lines.append("")

    if holdings:
        target_codes = meta.get("target_codes") or []
        if sell_mode == "index_rules":
            cls = classify_index_portfolio(holdings, target_codes, ranked)
            lines.append("### 相对持仓（index_rules）")
            lines.append("")
            lines.append(f"- **建议新增买入**：{', '.join(cls['buy_new']) or '无'}")
            lines.append(f"- **继续持有**：{', '.join(cls['hold_ok']) or '无'}")
            lines.append(f"- **关注调出**：{', '.join(cls['sell_watch']) or '无'}")
            if cls["not_in_pool"]:
                lines.append(f"- **未在候选池**：{', '.join(cls['not_in_pool'])}")
        else:
            buy_pool = show_df
            cls = classify_holdings(
                ranked, holdings, top_n=effective_top_n, sell_rank=sell_rank, buy_pool=buy_pool
            )
            lines.append("### 相对持仓（排名缓冲带）")
            lines.append("")
            lines.append(f"- **建议新增买入**：{', '.join(cls['buy_new']) or '无'}")
            lines.append(
                f"- **继续持有**（排名≤{sell_rank}）：{', '.join(cls['hold_ok']) or '无'}"
            )
            lines.append(
                f"- **关注卖出**（排名>{sell_rank}）：{', '.join(cls['sell_watch']) or '无'}"
            )
            if cls["not_in_pool"]:
                lines.append(f"- **未在候选池**：{', '.join(cls['not_in_pool'])}")
        lines.append("")

    lines.append("### 候选池统计")
    lines.append("")
    lines.append(f"- 全量排名：**{len(ranked)}** 只（预筛池内）")
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
    parser.add_argument(
        "--prefetch",
        type=int,
        default=BACKTEST_PREFETCH_SIZE,
        help=f"候选预筛数量（与回测一致，默认 {BACKTEST_PREFETCH_SIZE}）",
    )
    parser.add_argument("--holdings", type=str, help="持仓文件，每行一个代码")
    parser.add_argument(
        "--capital",
        type=float,
        default=None,
        help=(
            f"初始资金（元），用于建仓股数与佣金估算；"
            f"也可用环境变量 DLV_PORTFOLIO_CAPITAL_CNY（当前 {PORTFOLIO_CAPITAL_CNY:g}）；"
            f"均未设时默认 {BACKTEST_INITIAL_CAPITAL:,.0f} 元"
        ),
    )
    parser.add_argument(
        "--entry-date",
        type=str,
        default=None,
        help="建仓日 YYYY-MM-DD（默认今天）；用于建仓周年调仓日程",
    )
    parser.add_argument(
        "--rebalance-mode",
        choices=["entry_anniversary", "index_annual", "monthly", "quarterly_report", "fixed_days"],
        default=LIVE_REBALANCE_MODE,
        help=f"调仓日程（默认 {LIVE_REBALANCE_MODE}）",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        help="另存 Markdown 文件（如 output/dividend_lowvol/report.md）",
    )
    args = parser.parse_args(argv)

    holdings: list[str] | None = None
    if args.holdings:
        path = Path(args.holdings)
        if not path.exists():
            print(f"持仓文件不存在: {path}", file=sys.stderr)
            return 1
        holdings = parse_holdings_text(path.read_text(encoding="utf-8"))

    sell_rank = resolve_sell_rank(args.top, args.sell_rank)
    effective_capital = resolve_report_capital(args.capital)
    print(
        f"正在拉取数据（预筛 Top {args.prefetch}，目标 {args.top} 只，"
        f"初始资金 {effective_capital:,.0f} 元）…"
    )
    ranked, target_portfolio, meta = build_market_panel(
        refresh=args.refresh,
        top_n=args.top,
        sell_rank=sell_rank,
        prefetch_size=args.prefetch,
        holdings=holdings,
        entry_date=args.entry_date,
        rebalance_mode=args.rebalance_mode,
    )
    report = build_report(
        ranked,
        target_portfolio,
        meta,
        holdings=holdings,
        top_n=args.top,
        sell_rank=sell_rank,
        capital_cny=effective_capital,
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
