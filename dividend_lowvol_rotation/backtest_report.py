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


def build_trade_rounds(trades_df: pd.DataFrame, end_date: str) -> list[dict]:
    """从成交明细构建完整买卖回合（含未平仓）。"""
    if trades_df.empty:
        return []

    rounds: list[dict] = []
    open_buys: dict[str, list[dict]] = {}

    for _, row in trades_df.sort_values("date").iterrows():
        code = str(row["code"])
        if row["side"] == "买入":
            open_buys.setdefault(code, []).append(row.to_dict())
            continue

        buy_date = row.get("buy_date")
        buy_price = row.get("buy_price")
        hold_days = row.get("hold_days")
        ret = row.get("return_pct")
        rounds.append(
            {
                "code": code,
                "name": row.get("name", ""),
                "buy_date": str(buy_date) if buy_date else "",
                "sell_date": str(row["date"]),
                "hold_days": int(hold_days) if pd.notna(hold_days) else None,
                "shares": float(row["shares"]),
                "buy_price": float(buy_price) if buy_price else None,
                "sell_price": float(row["price"]),
                "buy_amount": float(buy_price) * float(row["shares"]) if buy_price else None,
                "sell_amount": float(row["amount"]),
                "pnl": float(row["realized_pnl"]) if pd.notna(row.get("realized_pnl")) else None,
                "return_pct": float(ret) if pd.notna(ret) else None,
                "status": "已平仓",
            }
        )
        if code in open_buys and open_buys[code]:
            open_buys[code].pop(0)

    for code, buys in open_buys.items():
        for b in buys:
            rounds.append(
                {
                    "code": code,
                    "name": b.get("name", ""),
                    "buy_date": str(b["date"]),
                    "sell_date": end_date,
                    "hold_days": (
                        pd.Timestamp(end_date) - pd.Timestamp(b["date"])
                    ).days,
                    "shares": float(b["shares"]),
                    "buy_price": float(b["price"]),
                    "sell_price": None,
                    "buy_amount": float(b["amount"]),
                    "sell_amount": None,
                    "pnl": None,
                    "return_pct": None,
                    "status": "持仓中",
                }
            )
    return rounds


def _enrich_rounds(rounds: list[dict]) -> list[dict]:
    for r in rounds:
        r["annualized_return_pct"] = _annualized_return(r.get("return_pct"), r.get("hold_days"))
    return rounds


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
        f"持仓 {meta.get('top_n')} 只 · 每 {meta.get('rebalance_days')} 交易日调仓",
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
    if meta.get("dividend_tax_enabled"):
        lines.extend(
            [
                "### 分红个税（税后收益）",
                "",
                "| 指标 | 数值 |",
                "|------|------|",
                f"| 税前现金分红 | {_fmt_money(meta.get('total_gross_dividend'))} 元 |",
                f"| 分红预扣税 | {_fmt_money(meta.get('total_dividend_tax'))} 元 |",
                f"| 分红计税次数 | {meta.get('dividend_tax_events', 0)} |",
                "",
                "税率：持股 ≤1 月 20%；1 月～1 年 10%；>1 年 0%。",
                "前复权价已含分红再投资，此处仅扣减税负拖累。",
                "",
            ]
        )
    lines.append("")
    if nav_df.empty:
        lines.append("无有效回测结果。")
        return "\n".join(lines)

    rounds = _enrich_rounds(build_trade_rounds(trades_df, meta.get("end", "")))

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
            "| 日期 | 方向 | 代码 | 名称 | 股数 | 单价 | 总价 | 持有天数 | 单笔收益 | 收益率 | 年化 |",
            "|------|------|------|------|------|------|------|----------|----------|--------|------|",
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
        ann = _annualized_return(float(ret) if pd.notna(ret) else None, hold)
        lines.append(
            f"| {row['date']} | {row['side']} | {row['code']} | {row['name']} | "
            f"{int(row['shares']):,} | {price:.4f} | {_fmt_money(total)} | {hold_s} | "
            f"{_fmt_money(float(pnl) if pd.notna(pnl) else None)} | "
            f"{_fmt_pct(float(ret) if pd.notna(ret) else None, 2)} | "
            f"{_fmt_pct(ann, 2)} |"
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
                "| 代码 | 名称 | 状态 | 已实现盈亏 | 未实现盈亏 | 总贡献 | 收益率 | 年化 | 平均持有天 | 最大回撤 |",
                "|------|------|------|------------|------------|--------|--------|------|------------|----------|",
            ]
        )
        for _, r in summary_df.iterrows():
            ret = r.get("realized_return_pct")
            days = r.get("avg_hold_days")
            ann = _annualized_return(float(ret) if pd.notna(ret) else None, days)
            lines.append(
                f"| {r['code']} | {r['name']} | {r['status']} | "
                f"{_fmt_money(r['realized_pnl'])} | {_fmt_money(r.get('unrealized_pnl'))} | "
                f"{_fmt_money(r['total_contribution_pnl'])} | "
                f"{_fmt_pct(float(ret) if pd.notna(ret) else None, 2)} | "
                f"{_fmt_pct(ann, 2)} | {r.get('avg_hold_days', '—')} | "
                f"{_fmt_pct(r['max_drawdown_pct'], 2)} |"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("完整交互图表请打开同目录 `backtest.html`。")
    return "\n".join(lines)


def render_backtest_html(
    nav_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    meta: dict,
    dividend_tax_df: pd.DataFrame | None = None,
) -> str:
    nav_dates = nav_df["date"].astype(str).tolist() if not nav_df.empty else []
    nav_vals = [float(v) for v in nav_df["nav"].tolist()] if not nav_df.empty else []

    rounds = _enrich_rounds(build_trade_rounds(trades_df, meta.get("end", "")))
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
        for r in rounds
    ]

    stock_rows = []
    if not summary_df.empty:
        for _, r in summary_df.iterrows():
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
                    "annualizedPct": _annualized_return(
                        float(ret) if pd.notna(ret) else None, days
                    ),
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
                "annualizedPct": _annualized_return(
                    float(ret) if pd.notna(ret) else None, hold
                ),
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

    payload = {
        "meta": meta,
        "nav": {"dates": nav_dates, "values": nav_vals},
        "timeline": timeline,
        "stocks": stock_rows,
        "trades": trade_rows,
        "dividendTax": dividend_rows,
    }
    data_json = json.dumps(payload, ensure_ascii=False)

    title = "红利低波轮动回测"
    subtitle = (
        f"{meta.get('start')} ~ {meta.get('end')} · "
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
    .chart.tall {{ height: 460px; }}
    table {{
      width: 100%; border-collapse: collapse; font-size: 0.82rem;
    }}
    th, td {{ border-bottom: 1px solid #eee; padding: 8px 10px; text-align: left; }}
    th {{ background: #fafafa; color: #555; font-weight: 600; }}
    tr:hover td {{ background: #fafcff; }}
    .buy {{ color: #cf1322; font-weight: 600; }}
    .sell {{ color: #389e0d; font-weight: 600; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .toolbar {{ margin-bottom: 10px; }}
    .toolbar input {{
      padding: 6px 10px; border: 1px solid #d9d9d9; border-radius: 6px; width: 220px;
    }}
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
    <h2>个股收益贡献</h2>
    <div id="pnlChart" class="chart"></div>
  </div>

  <div class="panel">
    <h2>持仓时间轴（买入 → 卖出 / 至今）</h2>
    <div id="timelineChart" class="chart tall"></div>
  </div>

  <div class="panel">
    <h2>买卖记录</h2>
    <div class="toolbar">
      <input id="tradeFilter" placeholder="筛选代码/名称…" />
    </div>
    <div style="overflow-x:auto">
      <table id="tradeTable">
        <thead>
          <tr>
            <th>日期</th><th>方向</th><th>代码</th><th>名称</th>
            <th class="num">股数</th><th class="num">单价</th><th class="num">总价</th>
            <th class="num">持有天</th><th class="num">收益</th><th class="num">收益率</th><th class="num">年化</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <div class="panel">
    <h2>个股收益汇总</h2>
    <div style="overflow-x:auto">
      <table id="stockTable">
        <thead>
          <tr>
            <th>代码</th><th>名称</th><th>状态</th>
            <th class="num">已实现</th><th class="num">未实现</th><th class="num">总贡献</th>
            <th class="num">收益率</th><th class="num">年化</th><th class="num">均持有天</th><th class="num">最大回撤</th>
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

    const meta = DATA.meta || {{}};
    const kpiDefs = [
      ['期末净值', meta.final_nav, '元', false],
      ['总收益率', meta.total_return_pct, '%', true],
      ['年化收益率', meta.cagr_pct, '%', true],
      ['最大回撤', meta.max_drawdown_pct, '%', false],
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

    const stocks = DATA.stocks || [];
    const pnlChart = echarts.init(document.getElementById('pnlChart'));
    pnlChart.setOption({{
      tooltip: {{
        trigger: 'axis',
        formatter: params => {{
          const p = params[0];
          const s = stocks[p.dataIndex];
          return `${{s.name}}(${{s.code}})<br/>总贡献: ${{fmt(s.total)}}<br/>收益率: ${{fmtPct(s.returnPct)}}<br/>年化: ${{fmtPct(s.annualizedPct)}}`;
        }}
      }},
      grid: {{ left: 60, right: 24, top: 24, bottom: 80 }},
      xAxis: {{
        type: 'category',
        data: stocks.map(s => s.name + '\\n' + s.code),
        axisLabel: {{ interval: 0, rotate: stocks.length > 12 ? 35 : 0, fontSize: 11 }}
      }},
      yAxis: {{ type: 'value', name: '盈亏(元)' }},
      series: [{{
        type: 'bar',
        data: stocks.map(s => ({{
          value: s.total,
          itemStyle: {{ color: s.total >= 0 ? '#cf1322' : '#389e0d' }}
        }}))
      }}]
    }});

    const timeline = DATA.timeline || [];
    const tlChart = echarts.init(document.getElementById('timelineChart'));
    const tlLabels = timeline.map(t => t.name + ' (' + t.code + ')');
    tlChart.setOption({{
      tooltip: {{
        formatter: p => {{
          const t = timeline[p.dataIndex];
          const lines = [
            t.name + ' ' + t.code,
            '买入: ' + t.buy + ' @ ' + fmt(t.buyPrice, 4),
            t.status === '持仓中' ? '至今持仓' : ('卖出: ' + t.sell + ' @ ' + fmt(t.sellPrice, 4)),
            '股数: ' + (t.shares != null ? Number(t.shares).toLocaleString('zh-CN') : '—'),
            '持有: ' + (t.days ?? '—') + ' 天',
          ];
          if (t.pnl !== null && t.pnl !== undefined) lines.push('收益: ' + fmt(t.pnl));
          if (t.ret !== null && t.ret !== undefined) lines.push('收益率: ' + fmtPct(t.ret));
          return lines.join('<br/>');
        }}
      }},
      grid: {{ left: 120, right: 40, top: 16, bottom: 24 }},
      xAxis: {{ type: 'time', min: meta.start, max: meta.end }},
      yAxis: {{ type: 'category', data: tlLabels, inverse: true }},
      series: [{{
        type: 'custom',
        renderItem: (params, api) => {{
          const idx = params.dataIndex;
          const t = timeline[idx];
          const y = api.coord([0, idx])[1];
          const x0 = api.coord([t.buy, idx])[0];
          const x1 = api.coord([t.sell, idx])[0];
          const h = 14;
          const color = t.status === '持仓中' ? '#1677ff' : ((t.pnl ?? 0) >= 0 ? '#cf1322' : '#389e0d');
          return {{
            type: 'rect',
            shape: {{ x: x0, y: y - h/2, width: Math.max(x1 - x0, 4), height: h }},
            style: {{ fill: color, opacity: 0.85 }}
          }};
        }},
        encode: {{ x: [1, 2], y: 0 }},
        data: timeline.map((t, i) => [i, t.buy, t.sell])
      }}]
    }});

    function renderTrades(filter='') {{
      const q = filter.trim().toLowerCase();
      const rows = (DATA.trades || []).filter(t =>
        !q || t.code.toLowerCase().includes(q) || t.name.toLowerCase().includes(q)
      );
      document.querySelector('#tradeTable tbody').innerHTML = rows.map(t => `
        <tr>
          <td>${{t.date}}</td>
          <td class="${{t.side === '买入' ? 'buy' : 'sell'}}">${{t.side}}</td>
          <td>${{t.code}}</td><td>${{t.name}}</td>
          <td class="num">${{t.shares != null ? Number(t.shares).toLocaleString('zh-CN') : '—'}}</td>
          <td class="num">${{fmt(t.price, 4)}}</td>
          <td class="num">${{fmt(t.amount, 2)}}</td>
          <td class="num">${{t.holdDays ?? '—'}}</td>
          <td class="num">${{t.pnl !== null ? fmt(t.pnl, 2) : '—'}}</td>
          <td class="num">${{fmtPct(t.returnPct)}}</td>
          <td class="num">${{fmtPct(t.annualizedPct)}}</td>
        </tr>`).join('');
    }}
    renderTrades();
    document.getElementById('tradeFilter').addEventListener('input', e => renderTrades(e.target.value));

    document.querySelector('#stockTable tbody').innerHTML = stocks.map(s => `
      <tr>
        <td>${{s.code}}</td><td>${{s.name}}</td><td>${{s.status}}</td>
        <td class="num">${{fmt(s.realized, 2)}}</td>
        <td class="num">${{fmt(s.unrealized, 2)}}</td>
        <td class="num">${{fmt(s.total, 2)}}</td>
        <td class="num">${{fmtPct(s.returnPct)}}</td>
        <td class="num">${{fmtPct(s.annualizedPct)}}</td>
        <td class="num">${{s.avgHoldDays !== null ? fmt(s.avgHoldDays, 1) : '—'}}</td>
        <td class="num">${{fmtPct(s.maxDdPct)}}</td>
      </tr>`).join('');

    window.addEventListener('resize', () => {{
      navChart.resize(); pnlChart.resize(); tlChart.resize();
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
) -> dict[str, Path]:
    del holdings_df  # 报告由 trades/summary 派生，不再单独落盘
    out_dir.mkdir(parents=True, exist_ok=True)
    md = format_backtest_report(nav_df, trades_df, summary_df, meta, dividend_tax_df)
    html = render_backtest_html(nav_df, trades_df, summary_df, meta, dividend_tax_df)
    paths = {
        "report": out_dir / "backtest.md",
        "html": out_dir / "backtest.html",
    }
    paths["report"].write_text(md, encoding="utf-8")
    paths["html"].write_text(html, encoding="utf-8")
    return paths
