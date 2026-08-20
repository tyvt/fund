# -*- coding: utf-8 -*-
"""回测报告：Markdown + HTML（ECharts 图表）。"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

import pandas as pd


def _fmt_money(v: float | None) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:,.2f}"


def _fmt_pct(v: float | None, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:.{digits}f}%"


def _annualized_return(return_pct: float | None, days: int | float | None) -> float | None:
    if return_pct is None or days is None or days <= 0:
        return None
    r = 1 + return_pct / 100
    years = days / 365.25
    if years <= 0:
        return None
    return (r ** (1 / years) - 1) * 100


def build_dividend_stock_summary(dividend_tax_df: pd.DataFrame | None) -> list[dict]:
    """按个股汇总分红：次数、税前、税额、税后。"""
    if dividend_tax_df is None or dividend_tax_df.empty:
        return []
    grouped = (
        dividend_tax_df.groupby(["code", "name"], as_index=False)
        .agg(
            events=("gross_dividend", "count"),
            gross=("gross_dividend", "sum"),
            tax=("tax_amount", "sum"),
            net=("net_dividend", "sum"),
        )
        .sort_values("net", ascending=False)
    )
    rows: list[dict] = []
    for _, r in grouped.iterrows():
        rows.append(
            {
                "code": str(r["code"]),
                "name": str(r["name"]),
                "events": int(r["events"]),
                "gross": float(r["gross"]),
                "tax": float(r["tax"]),
                "net": float(r["net"]),
            }
        )
    return rows


def build_trade_rounds(trades_df: pd.DataFrame, end_date: str) -> list[dict]:
    """从成交明细构建买卖回合（FIFO 按股数匹配，支持部分卖出）。"""
    if trades_df.empty:
        return []

    rounds: list[dict] = []
    open_lots: dict[str, list[dict]] = {}

    for _, row in trades_df.sort_values("date").iterrows():
        code = str(row["code"])
        side = str(row["side"])
        shares = int(row["shares"])
        if shares <= 0:
            continue

        if side == "买入":
            open_lots.setdefault(code, []).append(
                {
                    "code": code,
                    "name": row.get("name", ""),
                    "buy_date": str(row["date"]),
                    "buy_price": float(row["price"]),
                    "shares": shares,
                    "buy_amount": float(row["amount"]),
                }
            )
            continue

        sell_left = shares
        sell_pnl = float(row["realized_pnl"]) if pd.notna(row.get("realized_pnl")) else None
        sell_ret = float(row["return_pct"]) if pd.notna(row.get("return_pct")) else None
        pnl_left = sell_pnl
        while sell_left > 0 and open_lots.get(code):
            lot = open_lots[code][0]
            take = min(sell_left, int(lot["shares"]))
            if take <= 0:
                open_lots[code].pop(0)
                continue

            lot_pnl = None
            lot_ret = None
            if sell_pnl is not None and shares > 0:
                lot_pnl = sell_pnl * (take / shares)
                pnl_left = (pnl_left or 0) - lot_pnl
            if sell_ret is not None:
                lot_ret = sell_ret

            buy_amount = lot["buy_price"] * take
            sell_amount = float(row["price"]) * take
            hold_days = (
                pd.Timestamp(row["date"]) - pd.Timestamp(lot["buy_date"])
            ).days
            rounds.append(
                {
                    "code": code,
                    "name": row.get("name", "") or lot.get("name", ""),
                    "buy_date": lot["buy_date"],
                    "sell_date": str(row["date"]),
                    "hold_days": int(hold_days),
                    "shares": float(take),
                    "buy_price": float(lot["buy_price"]),
                    "sell_price": float(row["price"]),
                    "buy_amount": buy_amount,
                    "sell_amount": sell_amount,
                    "pnl": lot_pnl,
                    "return_pct": lot_ret,
                    "status": "已平仓",
                }
            )
            lot["shares"] -= take
            sell_left -= take
            if lot["shares"] <= 0:
                open_lots[code].pop(0)

    end_ts = pd.Timestamp(end_date)
    for code, lots in open_lots.items():
        for lot in lots:
            if int(lot["shares"]) <= 0:
                continue
            rounds.append(
                {
                    "code": code,
                    "name": lot.get("name", ""),
                    "buy_date": lot["buy_date"],
                    "sell_date": end_date,
                    "hold_days": (end_ts - pd.Timestamp(lot["buy_date"])).days,
                    "shares": float(lot["shares"]),
                    "buy_price": float(lot["buy_price"]),
                    "sell_price": None,
                    "buy_amount": float(lot["buy_price"]) * float(lot["shares"]),
                    "sell_amount": None,
                    "pnl": None,
                    "return_pct": None,
                    "status": "持仓中",
                }
            )

    closed = [r for r in rounds if r["status"] != "持仓中"]
    merged_open: dict[str, dict] = {}
    for r in rounds:
        if r["status"] != "持仓中":
            continue
        code = r["code"]
        if code not in merged_open:
            merged_open[code] = dict(r)
            continue
        cur = merged_open[code]
        total_shares = float(cur["shares"]) + float(r["shares"])
        buy_amt = float(cur.get("buy_amount") or 0) + float(r.get("buy_amount") or 0)
        cur["shares"] = total_shares
        cur["buy_amount"] = buy_amt
        cur["buy_price"] = buy_amt / total_shares if total_shares > 0 else cur["buy_price"]
        cur["buy_date"] = min(str(cur["buy_date"]), str(r["buy_date"]))
        cur["hold_days"] = (end_ts - pd.Timestamp(cur["buy_date"])).days
    return closed + list(merged_open.values())


def _enrich_rounds(rounds: list[dict]) -> list[dict]:
    for r in rounds:
        r["annualized_return_pct"] = _annualized_return(r.get("return_pct"), r.get("hold_days"))
    return rounds


def _sell_mode_label(meta: dict) -> str:
    mode = meta.get("sell_mode", "rank_buffer")
    if mode == "index_rules":
        return "指数硬门槛调出"
    sell_rank = meta.get("sell_rank")
    if sell_rank:
        return f"跌出前 {sell_rank} 卖"
    return "排名缓冲带调出"


def _rebalance_mode_label(meta: dict) -> str:
    mode = meta.get("rebalance_mode", "monthly")
    if mode == "index_annual":
        hold = meta.get("min_hold_days")
        suffix = f"，最短持有 {hold} 天" if hold else ""
        from dividend_lowvol_rotation.config import INDEX_ANNUAL_REBALANCE_TIMING

        timing = (
            "1月中旬"
            if INDEX_ANNUAL_REBALANCE_TIMING == "january"
            else "12月第二个周五次日"
        )
        return f"指数年度调仓({timing}){suffix}"
    if mode == "entry_anniversary":
        hold = meta.get("min_hold_days")
        suffix = f"，最短持有 {hold} 天" if hold else ""
        anchor = meta.get("entry_anchor") or meta.get("start")
        if anchor:
            return f"建仓周年调仓(建仓日 {anchor}){suffix}"
        return f"建仓周年调仓{suffix}"
    if mode == "monthly":
        return "每月首个交易日"
    if mode == "quarterly_report":
        return "季报截止后首个交易日"
    return f"每 {meta.get('rebalance_days')} 交易日调仓"


def build_yearly_stats(nav_df: pd.DataFrame, initial_capital: float) -> list[dict]:
    """按自然年汇总：年末净值、累计收益、当年收益与回撤。"""
    if nav_df.empty:
        return []
    df = nav_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    initial = float(initial_capital)
    prev_year_end_nav = initial
    stats: list[dict] = []
    for year in sorted(df["date"].dt.year.unique()):
        year_df = df[df["date"].dt.year == year]
        if year_df.empty:
            continue
        year_start_nav = prev_year_end_nav
        year_end_nav = float(year_df["nav"].iloc[-1])
        peak = year_start_nav
        annual_max_dd = 0.0
        for nav in year_df["nav"].astype(float):
            peak = max(peak, nav)
            if peak > 0:
                annual_max_dd = min(annual_max_dd, nav / peak - 1)
        stats.append(
            {
                "year": int(year),
                "yearEndNav": year_end_nav,
                "cumulativeReturnPct": (year_end_nav / initial - 1) * 100,
                "annualReturnPct": (year_end_nav / year_start_nav - 1) * 100 if year_start_nav > 0 else None,
                "annualMaxDdPct": annual_max_dd * 100,
                "annualProfit": year_end_nav - year_start_nav,
            }
        )
        prev_year_end_nav = year_end_nav
    return stats


def build_current_holdings(holdings_df: pd.DataFrame | None, summary_df: pd.DataFrame) -> list[dict]:
    """期末持仓快照（用于饼图）。"""
    rows: list[dict] = []
    if holdings_df is not None and not holdings_df.empty:
        last_date = holdings_df["date"].max()
        sub = holdings_df[holdings_df["date"] == last_date].copy()
        for _, r in sub.iterrows():
            mv = float(r["market_value"])
            rows.append(
                {
                    "code": str(r["code"]),
                    "name": str(r["name"]),
                    "shares": int(r["shares"]),
                    "price": float(r["price"]),
                    "marketValue": mv,
                    "weightPct": float(r["weight_pct"]) if pd.notna(r.get("weight_pct")) else None,
                    "buyDate": str(r.get("buy_date", "")),
                }
            )
    elif not summary_df.empty:
        sub = summary_df[summary_df["status"] == "持仓中"]
        for _, r in sub.iterrows():
            unreal = float(r.get("unrealized_pnl") or 0)
            real = float(r.get("realized_pnl") or 0)
            est_value = max(real + unreal, 0)
            rows.append(
                {
                    "code": str(r["code"]),
                    "name": str(r["name"]),
                    "shares": None,
                    "price": None,
                    "marketValue": est_value if est_value > 0 else None,
                    "weightPct": None,
                    "buyDate": "",
                }
            )
    rows.sort(key=lambda x: x.get("marketValue") or 0, reverse=True)
    total_mv = sum(x.get("marketValue") or 0 for x in rows)
    if total_mv > 0:
        for x in rows:
            if x.get("weightPct") is None and x.get("marketValue") is not None:
                x["weightPct"] = x["marketValue"] / total_mv * 100
    return rows


def format_backtest_report(
    nav_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    meta: dict,
    dividend_tax_df: pd.DataFrame | None = None,
) -> str:
    lines = [
        "# 红利低波轮动回测报告",
        "",
        f"> 区间 {meta.get('start')} ~ {meta.get('end')} · "
        f"持仓 {meta.get('top_n')} 只 · {_rebalance_mode_label(meta)}",
        "",
        "## 组合绩效",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 初始资金 | {_fmt_money(meta.get('initial_capital'))} 元 |",
        f"| 期末净值 | {_fmt_money(meta.get('final_nav'))} 元 |",
        f"| 总收益率 | {_fmt_pct(meta.get('total_return_pct'))} |",
        f"| 年化收益率 | {_fmt_pct(meta.get('cagr_pct'))} |",
        f"| 最大回撤 | {_fmt_pct(meta.get('max_drawdown_pct'))} |",
        f"| 成交笔数 | {meta.get('trade_count', 0)}（买 {meta.get('buy_count', 0)} / 卖 {meta.get('sell_count', 0)}） |",
        "",
    ]
    if meta.get("dividend_tax_enabled") or meta.get("dividend_cash_mode"):
        cash_mode = meta.get("dividend_cash_mode", False)
        lines.extend(
            [
                "### 分红与个税" if cash_mode else "### 分红个税（税后收益）",
                "",
                "| 指标 | 数值 |",
                "|------|------|",
                f"| 税前现金分红 | {_fmt_money(meta.get('total_gross_dividend'))} 元 |",
            ]
        )
        if meta.get("dividend_tax_enabled"):
            lines.append(f"| 分红预扣税 | {_fmt_money(meta.get('total_dividend_tax'))} 元 |")
            lines.append(f"| 税后现金分红 | {_fmt_money(meta.get('total_net_dividend'))} 元 |")
        lines.append(f"| 分红计税次数 | {meta.get('dividend_tax_events', 0)} |")
        lines.extend(
            [
                "",
                "税率：持股 ≤1 月 20%；1 月～1 年 10%；>1 年 0%。",
            ]
        )
        if cash_mode:
            lines.append("不复权价 + 税后现金分红计入资金池，可用于后续买入。")
        else:
            lines.append("前复权价已含分红再投资，此处仅扣减税负拖累。")
        lines.append("")
    lines.append("")
    if nav_df.empty:
        lines.append("无有效回测结果。")
        return "\n".join(lines)

    yearly = build_yearly_stats(nav_df, float(meta.get("initial_capital", 0)))
    if yearly:
        lines.extend(
            [
                "## 分年统计",
                "",
                "| 年份 | 年末净值 | 累计收益率 | 当年收益率 | 当年最大回撤 | 当年收益 |",
                "|------|----------|------------|------------|--------------|----------|",
            ]
        )
        for y in yearly:
            lines.append(
                f"| {y['year']} | {_fmt_money(y['yearEndNav'])} | "
                f"{_fmt_pct(y['cumulativeReturnPct'])} | {_fmt_pct(y['annualReturnPct'])} | "
                f"{_fmt_pct(y['annualMaxDdPct'])} | {_fmt_money(y['annualProfit'])} |"
            )
        lines.append("")

    rounds = build_trade_rounds(trades_df, meta.get("end", ""))

    # 净值走势 mermaid
    if len(nav_df) >= 2:
        lines.extend(["## 净值走势", "", "```mermaid", "xychart-beta", "    title \"组合净值\"", "    x-axis ["])
        step = max(1, len(nav_df) // 8)
        labels = [str(d)[:7] for d in nav_df["date"].iloc[::step]]
        lines.append("        " + ", ".join(f'"{l}"' for l in labels))
        lines.append("    ]")
        lines.append("    y-axis \"净值(元)\"")
        vals = [f"{v:.0f}" for v in nav_df["nav"].iloc[::step]]
        lines.append("    line [" + ", ".join(vals) + "]")
        lines.append("```")
        lines.append("")

    # 持仓时间轴 mermaid gantt
    if rounds:
        lines.extend(["## 持仓时间轴", "", "```mermaid", "gantt", "    title 个股持有区间", "    dateFormat YYYY-MM-DD", "    axisFormat %Y-%m", ""])
        for i, r in enumerate(rounds[:30]):
            title = f"{r['name']}({r['code']})"
            status = "active" if r["status"] == "持仓中" else "done"
            lines.append(f"    section {title}")
            lines.append(f"    {status}, {r['buy_date']}, {r['sell_date']}")
        if len(rounds) > 30:
            lines.append(f"    %% 另有 {len(rounds) - 30} 条持仓记录见 HTML 报告")
        lines.append("```")
        lines.append("")

    # 买卖明细
    lines.extend(
        [
            "## 买卖记录",
            "",
            "| 日期 | 方向 | 代码 | 名称 | 股数 | 单价 | 总价 | 持有天数 | 单笔收益 | 收益率 |",
            "|------|------|------|------|------|------|------|----------|----------|--------|",
        ]
    )
    for _, row in trades_df.sort_values("date").iterrows():
        total = float(row["amount"])
        shares = int(row["shares"])
        price = float(row["price"])
        hold = row.get("hold_days")
        hold_s = str(int(hold)) if pd.notna(hold) else "—"
        pnl = row.get("realized_pnl")
        ret = row.get("return_pct")
        lines.append(
            f"| {row['date']} | {row['side']} | {row['code']} | {row['name']} | "
            f"{int(row['shares']):,} | {price:.4f} | {_fmt_money(total)} | {hold_s} | "
            f"{_fmt_money(float(pnl) if pd.notna(pnl) else None)} | "
            f"{_fmt_pct(float(ret) if pd.notna(ret) else None, 2)} |"
        )
        lines.append("")

    if dividend_tax_df is not None and not dividend_tax_df.empty:
        lines.extend(
            [
                "## 分红扣税明细",
                "",
                "| 除权日 | 代码 | 名称 | 股数 | 每股分红 | 税前分红 | 持股天 | 税率档 | 税额 | 税后分红 |",
                "|--------|------|------|------|----------|----------|--------|--------|------|----------|",
            ]
        )
        for _, r in dividend_tax_df.sort_values("ex_date").iterrows():
            lines.append(
                f"| {r['ex_date']} | {r['code']} | {r['name']} | {int(r['shares']):,} | "
                f"{r['cash_per_share']:.4f} | {_fmt_money(r['gross_dividend'])} | {r['hold_days']} | "
                f"{r['tax_tier']} | {_fmt_money(r['tax_amount'])} | {_fmt_money(r['net_dividend'])} |"
            )
        lines.append("")

    # 个股汇总
    if not summary_df.empty:
        lines.extend(
            [
                "## 个股收益汇总",
                "",
                "| 代码 | 名称 | 状态 | 已实现盈亏 | 未实现盈亏 | 总贡献 | 收益率 | 平均持有天 | 最大回撤 |",
                "|------|------|------|------------|------------|--------|--------|------------|----------|",
            ]
        )
        for _, r in summary_df.iterrows():
            ret = r.get("realized_return_pct")
            days = r.get("avg_hold_days")
            lines.append(
                f"| {r['code']} | {r['name']} | {r['status']} | "
                f"{_fmt_money(r['realized_pnl'])} | {_fmt_money(r.get('unrealized_pnl'))} | "
                f"{_fmt_money(r['total_contribution_pnl'])} | "
                f"{_fmt_pct(float(ret) if pd.notna(ret) else None, 2)} | {r.get('avg_hold_days', '—')} | "
                f"{_fmt_pct(r['max_drawdown_pct'], 2)} |"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    html_name = meta.get("html_report_name", "backtest.html")
    lines.append(f"完整交互图表请打开同目录 `{html_name}`。")
    return "\n".join(lines)


def render_backtest_html(
    nav_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    holdings_df: pd.DataFrame | None,
    summary_df: pd.DataFrame,
    meta: dict,
    dividend_tax_df: pd.DataFrame | None = None,
) -> str:
    nav_dates = nav_df["date"].astype(str).tolist() if not nav_df.empty else []
    nav_vals = [float(v) for v in nav_df["nav"].tolist()] if not nav_df.empty else []

    rounds = build_trade_rounds(trades_df, meta.get("end", ""))
    timeline = [
        {
            "code": r["code"],
            "name": r["name"],
            "buy": r["buy_date"],
            "sell": r["sell_date"],
            "days": r.get("hold_days"),
            "pnl": r.get("pnl"),
            "ret": r.get("return_pct"),
            "status": r["status"],
            "shares": r.get("shares"),
            "buyPrice": r.get("buy_price"),
            "sellPrice": r.get("sell_price"),
            "buyAmount": r.get("buy_amount"),
            "sellAmount": r.get("sell_amount"),
        }
        for r in sorted(rounds, key=lambda x: x["buy_date"], reverse=True)
    ]

    stock_rows = []
    if not summary_df.empty:
        sorted_summary = summary_df.sort_values("total_contribution_pnl", ascending=False)
        for _, r in sorted_summary.iterrows():
            ret = r.get("realized_return_pct")
            days = r.get("avg_hold_days")
            stock_rows.append(
                {
                    "code": str(r["code"]),
                    "name": str(r["name"]),
                    "status": str(r["status"]),
                    "realized": float(r["realized_pnl"]),
                    "unrealized": float(r["unrealized_pnl"]) if pd.notna(r.get("unrealized_pnl")) else 0,
                    "total": float(r["total_contribution_pnl"]),
                    "returnPct": float(ret) if pd.notna(ret) else None,
                    "avgHoldDays": float(days) if pd.notna(days) else None,
                    "maxDdPct": float(r["max_drawdown_pct"]),
                }
            )

    trade_rows = []
    for _, row in trades_df.sort_values("date").iterrows():
        hold = row.get("hold_days")
        ret = row.get("return_pct")
        trade_rows.append(
            {
                "date": str(row["date"]),
                "side": str(row["side"]),
                "code": str(row["code"]),
                "name": str(row["name"]),
                "shares": float(row["shares"]),
                "price": float(row["price"]),
                "amount": float(row["amount"]),
                "holdDays": int(hold) if pd.notna(hold) else None,
                "pnl": float(row["realized_pnl"]) if pd.notna(row.get("realized_pnl")) else None,
                "returnPct": float(ret) if pd.notna(ret) else None,
                "buyDate": str(row["buy_date"]) if pd.notna(row.get("buy_date")) else None,
            }
        )

    dividend_rows = []
    if dividend_tax_df is not None and not dividend_tax_df.empty:
        for _, r in dividend_tax_df.sort_values("ex_date").iterrows():
            dividend_rows.append(
                {
                    "exDate": str(r["ex_date"]),
                    "code": str(r["code"]),
                    "name": str(r["name"]),
                    "shares": int(r["shares"]),
                    "cashPerShare": float(r["cash_per_share"]),
                    "gross": float(r["gross_dividend"]),
                    "holdDays": int(r["hold_days"]),
                    "tier": str(r["tax_tier"]),
                    "tax": float(r["tax_amount"]),
                    "net": float(r["net_dividend"]),
                }
            )

    yearly_stats = build_yearly_stats(nav_df, float(meta.get("initial_capital", 0)))
    current_holdings = build_current_holdings(holdings_df, summary_df)

    dividend_stock_rows = build_dividend_stock_summary(dividend_tax_df)

    payload = {
        "meta": meta,
        "nav": {"dates": nav_dates, "values": nav_vals},
        "yearly": yearly_stats,
        "holdings": current_holdings,
        "timeline": timeline,
        "stocks": stock_rows,
        "trades": trade_rows,
        "dividendTax": dividend_rows,
        "dividendStocks": dividend_stock_rows,
    }
    data_json = json.dumps(payload, ensure_ascii=False)

    title = "红利低波轮动回测"
    subtitle = (
        f"{meta.get('start')} ~ {meta.get('end')} · "
        f"{_rebalance_mode_label(meta)} · "
        f"初始 {meta.get('initial_capital', 0):,.0f} 元 · "
        f"总收益 {_fmt_pct(meta.get('total_return_pct'))} · "
        f"年化 {_fmt_pct(meta.get('cagr_pct'))}"
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC",
        "Microsoft YaHei", sans-serif;
      margin: 0; padding: 20px 24px 40px; background: #f0f2f5; color: #1f1f1f;
    }}
    h1 {{ margin: 0 0 6px; font-size: 1.5rem; }}
    .sub {{ color: #666; margin-bottom: 20px; font-size: 0.9rem; }}
    .kpis {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px; margin-bottom: 20px;
    }}
    .kpi {{
      background: #fff; border-radius: 10px; padding: 14px 16px;
      box-shadow: 0 1px 4px rgba(0,0,0,.06);
    }}
    .kpi .label {{ font-size: 0.78rem; color: #888; }}
    .kpi .val {{ font-size: 1.25rem; font-weight: 600; margin-top: 4px; }}
    .kpi .val.pos {{ color: #cf1322; }}
    .kpi .val.neg {{ color: #389e0d; }}
    .panel {{
      background: #fff; border-radius: 10px; padding: 16px;
      box-shadow: 0 1px 4px rgba(0,0,0,.06); margin-bottom: 20px;
    }}
    .panel h2 {{ margin: 0 0 12px; font-size: 1rem; }}
    .chart {{ width: 100%; height: 380px; }}
    .chart.pie {{ height: 360px; }}
    .grid2 {{
      display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
    }}
    @media (max-width: 900px) {{ .grid2 {{ grid-template-columns: 1fr; }} }}
    table {{
      width: 100%; border-collapse: collapse; font-size: 0.82rem;
    }}
    th, td {{ border-bottom: 1px solid #eee; padding: 8px 10px; text-align: left; }}
    th {{ background: #fafafa; color: #555; font-weight: 600; user-select: none; }}
    th.sortable {{ cursor: pointer; }}
    th.sortable:hover {{ color: #1677ff; }}
    th.sortable::after {{ content: ' ⇅'; color: #ccc; font-size: 0.7rem; }}
    th.sort-asc::after {{ content: ' ↑'; color: #1677ff; }}
    th.sort-desc::after {{ content: ' ↓'; color: #1677ff; }}
    tr:hover td {{ background: #fafcff; }}
    .buy {{ color: #cf1322; font-weight: 600; }}
    .sell {{ color: #389e0d; font-weight: 600; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .toolbar {{
      display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 10px;
    }}
    .toolbar input, .toolbar select {{
      padding: 6px 10px; border: 1px solid #d9d9d9; border-radius: 6px; font-size: 0.85rem;
    }}
    .toolbar label {{ font-size: 0.82rem; color: #666; }}
    .badge {{
      display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem;
    }}
    .badge.hold {{ background: #e6f4ff; color: #1677ff; }}
    .badge.closed {{ background: #f6ffed; color: #389e0d; }}
    .timeline-range {{
      font-size: 0.78rem; color: #888; margin-top: 2px;
    }}
    .pos {{ color: #cf1322; }}
    .neg {{ color: #389e0d; }}
  </style>
</head>
<body>
  <h1>{escape(title)}</h1>
  <p class="sub">{escape(subtitle)}</p>

  <div class="kpis" id="kpiRow"></div>

  <div class="panel">
    <h2>组合净值曲线</h2>
    <div id="navChart" class="chart"></div>
  </div>

  <div class="panel">
    <h2>分年统计</h2>
    <div style="overflow-x:auto">
      <table id="yearlyTable">
        <thead>
          <tr>
            <th>年份</th>
            <th class="num">年末净值</th>
            <th class="num">累计收益率</th>
            <th class="num">当年收益率</th>
            <th class="num">当年最大回撤</th>
            <th class="num">当年收益</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <div class="panel" id="dividendStockPanel" style="display:none">
    <h2>个股分红收益</h2>
    <p class="timeline-range" id="dividendStockSummary"></p>
    <div style="overflow-x:auto">
      <table id="dividendStockTable">
        <thead>
          <tr>
            <th>代码</th><th>名称</th>
            <th class="num sortable" data-sort="events">分红次数</th>
            <th class="num sortable" data-sort="gross">税前分红</th>
            <th class="num sortable" data-sort="tax">预扣税</th>
            <th class="num sortable" data-sort="net">税后分红</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <div class="panel" id="dividendDetailPanel" style="display:none">
    <h2>分红明细</h2>
    <div class="toolbar">
      <input id="dividendFilter" placeholder="筛选代码/名称…" style="width:160px" />
    </div>
    <div style="overflow-x:auto">
      <table id="dividendDetailTable">
        <thead>
          <tr>
            <th class="sortable" data-sort="exDate">除权日</th><th>代码</th><th>名称</th>
            <th class="num">股数</th><th class="num">每股分红</th>
            <th class="num sortable" data-sort="gross">税前</th>
            <th class="num">持股天</th><th>税率档</th>
            <th class="num sortable" data-sort="tax">税额</th>
            <th class="num sortable" data-sort="net">税后</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <div class="grid2">
    <div class="panel">
      <h2>当前持仓</h2>
      <div id="holdingsPie" class="chart pie"></div>
    </div>
    <div class="panel">
      <h2>当前持仓明细</h2>
      <div style="overflow-x:auto">
        <table id="holdingsTable">
          <thead>
            <tr>
              <th>代码</th><th>名称</th><th class="num">股数</th><th class="num">现价</th>
              <th class="num">市值</th><th class="num">权重</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="panel">
    <h2>个股收益贡献</h2>
    <div style="overflow-x:auto">
      <table id="stockTable">
        <thead>
          <tr>
            <th>代码</th><th>名称</th><th>状态</th>
            <th class="num">已实现</th><th class="num">未实现</th>
            <th class="num sortable" data-sort="total">总贡献</th>
            <th class="num sortable" data-sort="returnPct">收益率</th>
            <th class="num">均持有天</th><th class="num">最大回撤</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <div class="panel">
    <h2>持仓回合</h2>
    <div class="toolbar">
      <label>买入起 <input type="date" id="tlStart" /></label>
      <label>买入止 <input type="date" id="tlEnd" /></label>
      <input id="tlFilter" placeholder="筛选代码/名称…" style="width:160px" />
    </div>
    <div style="overflow-x:auto">
      <table id="timelineTable">
        <thead>
          <tr>
            <th>代码</th><th>名称</th><th>状态</th><th>买入日</th><th>卖出日</th>
            <th class="num">持有天</th><th class="num">股数</th>
            <th class="num">买入价</th><th class="num">卖出价</th>
            <th class="num sortable" data-sort="pnl">收益</th>
            <th class="num sortable" data-sort="ret">收益率</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <div class="panel">
    <h2>买卖记录</h2>
    <div class="toolbar">
      <label>起 <input type="date" id="tradeStart" /></label>
      <label>止 <input type="date" id="tradeEnd" /></label>
      <input id="tradeFilter" placeholder="筛选代码/名称…" style="width:160px" />
    </div>
    <div style="overflow-x:auto">
      <table id="tradeTable">
        <thead>
          <tr>
            <th class="sortable" data-sort="date">日期</th><th>方向</th><th>代码</th><th>名称</th>
            <th class="num">股数</th><th class="num">单价</th><th class="num">总价</th>
            <th class="num">持有天</th>
            <th class="num sortable" data-sort="pnl">收益</th>
            <th class="num sortable" data-sort="returnPct">收益率</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <script>
    const DATA = {data_json};

    function fmt(n, d=2) {{
      if (n === null || n === undefined || Number.isNaN(n)) return '—';
      return Number(n).toLocaleString('zh-CN', {{minimumFractionDigits: d, maximumFractionDigits: d}});
    }}
    function fmtPct(n) {{
      if (n === null || n === undefined) return '—';
      return fmt(n, 2) + '%';
    }}
    function pnlClass(n) {{
      if (n === null || n === undefined) return '';
      return n >= 0 ? 'pos' : 'neg';
    }}

    const meta = DATA.meta || {{}};

    const kpiDefs = [
      ['期末净值', meta.final_nav, '元', false],
      ['总收益率', meta.total_return_pct, '%', true],
      ['年化收益率', meta.cagr_pct, '%', true],
      ['最大回撤', meta.max_drawdown_pct, '%', false],
      ['期末持仓', meta.holdings_count, '只', false],
      ...(meta.total_gross_dividend ? [['税前分红', meta.total_gross_dividend, '元', false]] : []),
      ...(meta.total_net_dividend !== undefined && meta.total_net_dividend !== null
        ? [['税后分红', meta.total_net_dividend, '元', false]] : []),
      ...(meta.dividend_tax_enabled ? [['分红预扣税', meta.total_dividend_tax, '元', false]] : []),
      ['成交笔数', meta.trade_count, '', false],
    ].filter(k => k[1] !== null && k[1] !== undefined);
    document.getElementById('kpiRow').innerHTML = kpiDefs.map(([label, val, unit, colorize]) => {{
      const cls = colorize ? (val >= 0 ? 'pos' : 'neg') : '';
      const text = unit === '%' ? fmtPct(val) : (unit === '元' ? fmt(val, 0) + ' 元' : val);
      return `<div class="kpi"><div class="label">${{label}}</div><div class="val ${{cls}}">${{text}}</div></div>`;
    }}).join('');

    const navChart = echarts.init(document.getElementById('navChart'));
    navChart.setOption({{
      tooltip: {{ trigger: 'axis' }},
      grid: {{ left: 60, right: 24, top: 24, bottom: 48 }},
      xAxis: {{ type: 'category', data: DATA.nav.dates }},
      yAxis: {{ type: 'value', scale: true, name: '净值(元)' }},
      series: [{{
        type: 'line', data: DATA.nav.values, smooth: true,
        areaStyle: {{ opacity: 0.08 }}, lineStyle: {{ width: 2 }},
        itemStyle: {{ color: '#1677ff' }}
      }}]
    }});

    const yearly = DATA.yearly || [];
    document.querySelector('#yearlyTable tbody').innerHTML = yearly.map(y => `
      <tr>
        <td>${{y.year}}</td>
        <td class="num">${{fmt(y.yearEndNav, 2)}}</td>
        <td class="num ${{pnlClass(y.cumulativeReturnPct)}}">${{fmtPct(y.cumulativeReturnPct)}}</td>
        <td class="num ${{pnlClass(y.annualReturnPct)}}">${{fmtPct(y.annualReturnPct)}}</td>
        <td class="num">${{fmtPct(y.annualMaxDdPct)}}</td>
        <td class="num ${{pnlClass(y.annualProfit)}}">${{fmt(y.annualProfit, 2)}}</td>
      </tr>`).join('');

    const holdings = DATA.holdings || [];
    const holdingsPie = echarts.init(document.getElementById('holdingsPie'));
    if (holdings.length) {{
      holdingsPie.setOption({{
        tooltip: {{
          trigger: 'item',
          formatter: p => `${{p.name}}<br/>市值: ${{fmt(p.value, 2)}}<br/>占比: ${{fmt(p.percent, 1)}}%`
        }},
        legend: {{ type: 'scroll', orient: 'vertical', right: 8, top: 16, bottom: 16 }},
        series: [{{
          type: 'pie', radius: ['38%', '68%'], center: ['38%', '50%'],
          data: holdings.map(h => ({{
            name: h.name + '(' + h.code + ')',
            value: h.marketValue || 0
          }})),
          label: {{ formatter: '{{b}}: {{d}}%' }},
          emphasis: {{ itemStyle: {{ shadowBlur: 8, shadowColor: 'rgba(0,0,0,.12)' }} }}
        }}]
      }});
    }} else {{
      document.getElementById('holdingsPie').innerHTML = '<p style="color:#999;padding:40px;text-align:center">无持仓</p>';
    }}
    document.querySelector('#holdingsTable tbody').innerHTML = holdings.map(h => `
      <tr>
        <td>${{h.code}}</td><td>${{h.name}}</td>
        <td class="num">${{h.shares != null ? Number(h.shares).toLocaleString('zh-CN') : '—'}}</td>
        <td class="num">${{h.price != null ? fmt(h.price, 4) : '—'}}</td>
        <td class="num">${{h.marketValue != null ? fmt(h.marketValue, 2) : '—'}}</td>
        <td class="num">${{h.weightPct != null ? fmtPct(h.weightPct) : '—'}}</td>
      </tr>`).join('');

    let stockSort = {{ key: 'total', dir: 'desc' }};
    function renderStocks() {{
      const stocks = [...(DATA.stocks || [])];
      const k = stockSort.key;
      const dir = stockSort.dir === 'asc' ? 1 : -1;
      stocks.sort((a, b) => {{
        const av = a[k] ?? -Infinity;
        const bv = b[k] ?? -Infinity;
        return (av - bv) * dir;
      }});
      document.querySelector('#stockTable tbody').innerHTML = stocks.map(s => `
        <tr>
          <td>${{s.code}}</td><td>${{s.name}}</td><td>${{s.status}}</td>
          <td class="num">${{fmt(s.realized, 2)}}</td>
          <td class="num">${{fmt(s.unrealized, 2)}}</td>
          <td class="num ${{pnlClass(s.total)}}">${{fmt(s.total, 2)}}</td>
          <td class="num">${{fmtPct(s.returnPct)}}</td>
          <td class="num">${{s.avgHoldDays !== null ? fmt(s.avgHoldDays, 1) : '—'}}</td>
          <td class="num">${{fmtPct(s.maxDdPct)}}</td>
        </tr>`).join('');
    }}
    renderStocks();
    document.querySelectorAll('#stockTable th.sortable').forEach(th => {{
      th.addEventListener('click', () => {{
        const key = th.dataset.sort;
        if (stockSort.key === key) stockSort.dir = stockSort.dir === 'asc' ? 'desc' : 'asc';
        else {{ stockSort.key = key; stockSort.dir = 'desc'; }}
        document.querySelectorAll('#stockTable th.sortable').forEach(t => {{
          t.classList.remove('sort-asc', 'sort-desc');
          if (t.dataset.sort === stockSort.key) t.classList.add(stockSort.dir === 'asc' ? 'sort-asc' : 'sort-desc');
        }});
        renderStocks();
      }});
    }});
    document.querySelector('#stockTable th[data-sort="total"]').classList.add('sort-desc');

    let tlSort = {{ key: 'pnl', dir: 'desc' }};
    function renderTimeline() {{
      const q = (document.getElementById('tlFilter').value || '').trim().toLowerCase();
      const start = document.getElementById('tlStart').value;
      const end = document.getElementById('tlEnd').value;
      let rows = (DATA.timeline || []).filter(t => {{
        if (q && !t.code.toLowerCase().includes(q) && !t.name.toLowerCase().includes(q)) return false;
        if (start && t.buy < start) return false;
        if (end && t.buy > end) return false;
        return true;
      }});
      const k = tlSort.key;
      const dir = tlSort.dir === 'asc' ? 1 : -1;
      rows.sort((a, b) => {{
        const av = a[k] ?? (k === 'buy' ? '' : -Infinity);
        const bv = b[k] ?? (k === 'buy' ? '' : -Infinity);
        if (k === 'buy') return av.localeCompare(bv) * dir;
        return ((av ?? -Infinity) - (bv ?? -Infinity)) * dir;
      }});
      document.querySelector('#timelineTable tbody').innerHTML = rows.map(t => `
        <tr>
          <td>${{t.code}}</td>
          <td>${{t.name}}</td>
          <td><span class="badge ${{t.status === '持仓中' ? 'hold' : 'closed'}}">${{t.status}}</span></td>
          <td>${{t.buy}}</td>
          <td>${{t.status === '持仓中' ? '—' : t.sell}}</td>
          <td class="num">${{t.days ?? '—'}}</td>
          <td class="num">${{t.shares != null ? Number(t.shares).toLocaleString('zh-CN') : '—'}}</td>
          <td class="num">${{t.buyPrice != null ? fmt(t.buyPrice, 4) : '—'}}</td>
          <td class="num">${{t.sellPrice != null ? fmt(t.sellPrice, 4) : '—'}}</td>
          <td class="num ${{pnlClass(t.pnl)}}">${{t.pnl !== null && t.pnl !== undefined ? fmt(t.pnl, 2) : '—'}}</td>
          <td class="num">${{fmtPct(t.ret)}}</td>
        </tr>`).join('');
    }}
    renderTimeline();
    document.getElementById('tlFilter').addEventListener('input', renderTimeline);
    document.getElementById('tlStart').addEventListener('change', renderTimeline);
    document.getElementById('tlEnd').addEventListener('change', renderTimeline);
    document.querySelectorAll('#timelineTable th.sortable').forEach(th => {{
      th.addEventListener('click', () => {{
        const key = th.dataset.sort;
        if (tlSort.key === key) tlSort.dir = tlSort.dir === 'asc' ? 'desc' : 'asc';
        else {{ tlSort.key = key; tlSort.dir = 'desc'; }}
        document.querySelectorAll('#timelineTable th.sortable').forEach(t => {{
          t.classList.remove('sort-asc', 'sort-desc');
          if (t.dataset.sort === tlSort.key) t.classList.add(tlSort.dir === 'asc' ? 'sort-asc' : 'sort-desc');
        }});
        renderTimeline();
      }});
    }});

    let tradeSort = {{ key: 'date', dir: 'desc' }};
    function renderTrades() {{
      const q = (document.getElementById('tradeFilter').value || '').trim().toLowerCase();
      const start = document.getElementById('tradeStart').value;
      const end = document.getElementById('tradeEnd').value;
      let rows = (DATA.trades || []).filter(t => {{
        if (q && !t.code.toLowerCase().includes(q) && !t.name.toLowerCase().includes(q)) return false;
        if (start && t.date < start) return false;
        if (end && t.date > end) return false;
        return true;
      }});
      const k = tradeSort.key;
      const dir = tradeSort.dir === 'asc' ? 1 : -1;
      rows.sort((a, b) => {{
        const av = a[k] ?? (k === 'date' ? '' : -Infinity);
        const bv = b[k] ?? (k === 'date' ? '' : -Infinity);
        if (k === 'date') return av.localeCompare(bv) * dir;
        return ((av ?? -Infinity) - (bv ?? -Infinity)) * dir;
      }});
      document.querySelector('#tradeTable tbody').innerHTML = rows.map(t => `
        <tr>
          <td>${{t.date}}</td>
          <td class="${{t.side === '买入' ? 'buy' : 'sell'}}">${{t.side}}</td>
          <td>${{t.code}}</td><td>${{t.name}}</td>
          <td class="num">${{t.shares != null ? Number(t.shares).toLocaleString('zh-CN') : '—'}}</td>
          <td class="num">${{fmt(t.price, 4)}}</td>
          <td class="num">${{fmt(t.amount, 2)}}</td>
          <td class="num">${{t.holdDays ?? '—'}}</td>
          <td class="num ${{pnlClass(t.pnl)}}">${{t.pnl !== null ? fmt(t.pnl, 2) : '—'}}</td>
          <td class="num">${{fmtPct(t.returnPct)}}</td>
        </tr>`).join('');
    }}
    renderTrades();
    document.getElementById('tradeFilter').addEventListener('input', renderTrades);
    document.getElementById('tradeStart').addEventListener('change', renderTrades);
    document.getElementById('tradeEnd').addEventListener('change', renderTrades);
    document.querySelectorAll('#tradeTable th.sortable').forEach(th => {{
      th.addEventListener('click', () => {{
        const key = th.dataset.sort;
        if (tradeSort.key === key) tradeSort.dir = tradeSort.dir === 'asc' ? 'desc' : 'asc';
        else {{ tradeSort.key = key; tradeSort.dir = key === 'date' ? 'desc' : 'desc'; }}
        document.querySelectorAll('#tradeTable th.sortable').forEach(t => {{
          t.classList.remove('sort-asc', 'sort-desc');
          if (t.dataset.sort === tradeSort.key) t.classList.add(tradeSort.dir === 'asc' ? 'sort-asc' : 'sort-desc');
        }});
        renderTrades();
      }});
    }});
    document.querySelector('#tradeTable th[data-sort="date"]').classList.add('sort-desc');

    const dividendStocks = DATA.dividendStocks || [];
    const dividendEvents = DATA.dividendTax || [];
    if (dividendStocks.length) {{
      document.getElementById('dividendStockPanel').style.display = '';
      const totalGross = dividendStocks.reduce((s, r) => s + r.gross, 0);
      const totalTax = dividendStocks.reduce((s, r) => s + r.tax, 0);
      const totalNet = dividendStocks.reduce((s, r) => s + r.net, 0);
      const totalEvents = dividendEvents.length;
      document.getElementById('dividendStockSummary').textContent =
        `全组合税前 ${{fmt(totalGross, 2)}} 元 · 预扣税 ${{fmt(totalTax, 2)}} 元 · 税后 ${{fmt(totalNet, 2)}} 元 · 共 ${{totalEvents}} 次`;
    }}
    if (dividendEvents.length) {{
      document.getElementById('dividendDetailPanel').style.display = '';
    }}

    let divStockSort = {{ key: 'net', dir: 'desc' }};
    function renderDividendStocks() {{
      const rows = [...dividendStocks];
      const k = divStockSort.key;
      const dir = divStockSort.dir === 'asc' ? 1 : -1;
      rows.sort((a, b) => ((a[k] ?? -Infinity) - (b[k] ?? -Infinity)) * dir);
      document.querySelector('#dividendStockTable tbody').innerHTML = rows.map(r => `
        <tr>
          <td>${{r.code}}</td><td>${{r.name}}</td>
          <td class="num">${{r.events}}</td>
          <td class="num">${{fmt(r.gross, 2)}}</td>
          <td class="num">${{fmt(r.tax, 2)}}</td>
          <td class="num pos">${{fmt(r.net, 2)}}</td>
        </tr>`).join('');
    }}
    if (dividendStocks.length) {{
      renderDividendStocks();
      document.querySelectorAll('#dividendStockTable th.sortable').forEach(th => {{
        th.addEventListener('click', () => {{
          const key = th.dataset.sort;
          if (divStockSort.key === key) divStockSort.dir = divStockSort.dir === 'asc' ? 'desc' : 'asc';
          else {{ divStockSort.key = key; divStockSort.dir = 'desc'; }}
          document.querySelectorAll('#dividendStockTable th.sortable').forEach(t => {{
            t.classList.remove('sort-asc', 'sort-desc');
            if (t.dataset.sort === divStockSort.key) t.classList.add(divStockSort.dir === 'asc' ? 'sort-asc' : 'sort-desc');
          }});
          renderDividendStocks();
        }});
      }});
      document.querySelector('#dividendStockTable th[data-sort="net"]').classList.add('sort-desc');
    }}

    let divDetailSort = {{ key: 'exDate', dir: 'asc' }};
    function renderDividendDetails() {{
      const q = (document.getElementById('dividendFilter')?.value || '').trim().toLowerCase();
      let rows = dividendEvents.filter(r => {{
        if (!q) return true;
        return r.code.toLowerCase().includes(q) || r.name.toLowerCase().includes(q);
      }});
      const k = divDetailSort.key;
      const dir = divDetailSort.dir === 'asc' ? 1 : -1;
      rows.sort((a, b) => {{
        const av = a[k] ?? (k === 'exDate' ? '' : -Infinity);
        const bv = b[k] ?? (k === 'exDate' ? '' : -Infinity);
        if (k === 'exDate') return av.localeCompare(bv) * dir;
        return ((av ?? -Infinity) - (bv ?? -Infinity)) * dir;
      }});
      document.querySelector('#dividendDetailTable tbody').innerHTML = rows.map(r => `
        <tr>
          <td>${{r.exDate}}</td><td>${{r.code}}</td><td>${{r.name}}</td>
          <td class="num">${{Number(r.shares).toLocaleString('zh-CN')}}</td>
          <td class="num">${{fmt(r.cashPerShare, 4)}}</td>
          <td class="num">${{fmt(r.gross, 2)}}</td>
          <td class="num">${{r.holdDays}}</td>
          <td>${{r.tier}}</td>
          <td class="num">${{fmt(r.tax, 2)}}</td>
          <td class="num pos">${{fmt(r.net, 2)}}</td>
        </tr>`).join('');
    }}
    if (dividendEvents.length) {{
      renderDividendDetails();
      document.getElementById('dividendFilter').addEventListener('input', renderDividendDetails);
      document.querySelectorAll('#dividendDetailTable th.sortable').forEach(th => {{
        th.addEventListener('click', () => {{
          const key = th.dataset.sort;
          if (divDetailSort.key === key) divDetailSort.dir = divDetailSort.dir === 'asc' ? 'desc' : 'asc';
          else {{ divDetailSort.key = key; divDetailSort.dir = key === 'exDate' ? 'asc' : 'desc'; }}
          document.querySelectorAll('#dividendDetailTable th.sortable').forEach(t => {{
            t.classList.remove('sort-asc', 'sort-desc');
            if (t.dataset.sort === divDetailSort.key) t.classList.add(divDetailSort.dir === 'asc' ? 'sort-asc' : 'sort-desc');
          }});
          renderDividendDetails();
        }});
      }});
      document.querySelector('#dividendDetailTable th[data-sort="exDate"]').classList.add('sort-asc');
    }}

    if (meta.start) {{
      document.getElementById('tradeStart').value = meta.start;
      document.getElementById('tlStart').value = meta.start;
    }}
    if (meta.end) {{
      document.getElementById('tradeEnd').value = meta.end;
      document.getElementById('tlEnd').value = meta.end;
    }}

    window.addEventListener('resize', () => {{
      navChart.resize();
      if (holdings.length) holdingsPie.resize();
    }});
  </script>
</body>
</html>"""


def save_backtest_outputs(
    out_dir: Path,
    nav_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    holdings_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    meta: dict,
    dividend_tax_df: pd.DataFrame | None = None,
    *,
    report_basename: str = "backtest",
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    html_name = f"{report_basename}.html"
    meta = dict(meta)
    meta.setdefault("html_report_name", html_name)
    meta.setdefault("report_basename", report_basename)
    md = format_backtest_report(nav_df, trades_df, summary_df, meta, dividend_tax_df)
    html = render_backtest_html(
        nav_df, trades_df, holdings_df, summary_df, meta, dividend_tax_df
    )
    paths = {
        "report": out_dir / f"{report_basename}.md",
        "html": out_dir / html_name,
    }
    paths["report"].write_text(md, encoding="utf-8")
    paths["html"].write_text(html, encoding="utf-8")
    return paths
