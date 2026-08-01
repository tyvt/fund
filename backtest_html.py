"""回测 HTML 折线图：收盘价走势 + 买入/卖出标记，支持时间范围筛选。"""

import json
from datetime import date
from html import escape

import pandas as pd


def append_sell_dates_column(table: pd.DataFrame, sell_dates: list) -> pd.DataFrame:
    """在逐日表上标注实际卖出日期（与波段回测一致）。"""
    if table is None or table.empty or not sell_dates:
        return table
    sell_set = set(sell_dates)
    out = table.copy()
    out["sell"] = out["date"].map(lambda d: "卖出" if d in sell_set else "")
    return out


def _table_to_series(table: pd.DataFrame) -> dict:
    """将逐日表转为图表序列。"""
    if table is None or table.empty:
        return {
            "dates": [],
            "closes": [],
            "buys": [],
            "sells": [],
            "buy_amounts": [],
        }

    has_amount = "buy_amount" in table.columns
    dates = []
    closes = []
    buys = []
    sells = []
    buy_amounts = []
    for _, row in table.iterrows():
        dates.append(str(row["date"]))
        close_val = row.get("close")
        closes.append(float(close_val) if pd.notna(close_val) else None)
        is_buy = row.get("buy") == "买入"
        buys.append(is_buy)
        sell_flag = row.get("sell")
        sells.append(sell_flag in ("卖出", True))
        if has_amount and is_buy:
            amt = row.get("buy_amount")
            buy_amounts.append(float(amt) if pd.notna(amt) else 0.0)
        else:
            buy_amounts.append(0.0)
    return {
        "dates": dates,
        "closes": closes,
        "buys": buys,
        "sells": sells,
        "buy_amounts": buy_amounts,
    }


def build_chart_payload(daily_tables: list[dict]) -> dict:
    """汇总各指数图表数据。"""
    payload = {}
    for item in daily_tables:
        code = item["code"]
        payload[code] = {
            "name": item["name"],
            "code": code,
            **_table_to_series(item.get("table")),
        }
    return payload


def resolve_return_pct_by_code(amounts=None, rows=None) -> dict[str, float | None]:
    """解析各指数策略收益率，用于 HTML 下拉排序。"""
    if amounts and amounts.get("ranking_rows"):
        return {
            r["code"]: r.get("return_pct") for r in amounts["ranking_rows"]
        }
    if not rows:
        return {}
    out: dict[str, float | None] = {}
    for row in rows:
        if isinstance(row, dict):
            out[row["code"]] = row.get("return_pct")
        else:
            ret = row.return_pct if row.has_sell else row.buy_only_return_pct
            out[row.code] = ret
    return out


def sort_codes_by_return(
    codes: list[str], return_pct_by_code: dict[str, float | None] | None
) -> list[str]:
    """按策略收益率从高到低排序；无收益率的排在末尾。"""
    if not return_pct_by_code:
        return codes

    def sort_key(code: str):
        ret = return_pct_by_code.get(code)
        if ret is None:
            return (1, 0.0, code)
        return (0, -float(ret), code)

    return sorted(codes, key=sort_key)


def render_backtest_html(
    title: str,
    daily_tables: list[dict],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    subtitle: str = "",
    return_pct_by_code: dict[str, float | None] | None = None,
) -> str:
    """生成含交互折线图的自包含 HTML。"""
    payload = build_chart_payload(daily_tables)
    if not payload:
        return (
            "<!DOCTYPE html><html lang='zh-CN'><head>"
            "<meta charset='UTF-8'><title>无数据</title></head>"
            "<body><p>无可用图表数据</p></body></html>"
        )

    codes = sort_codes_by_return(list(payload.keys()), return_pct_by_code)
    default_code = codes[0]
    all_dates = sorted(
        {d for s in payload.values() for d in s["dates"] if d}
    )
    today = date.today()
    default_filter_start = all_dates[0] if all_dates else today.isoformat()
    default_filter_end = all_dates[-1] if all_dates else today.isoformat()

    data_json = json.dumps(payload, ensure_ascii=False)
    page_title = title
    page_subtitle = subtitle or "买入标准：当前 config 阈值"
    options_html = "\n".join(
        f'<option value="{escape(c)}">{escape(payload[c]["name"])} ({escape(c)})</option>'
        for c in codes
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(page_title)}</title>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC",
        "Microsoft YaHei", sans-serif;
      margin: 0; padding: 16px 20px 32px; background: #f5f6f8; color: #1a1a1a;
    }}
    h1 {{ font-size: 1.35rem; margin: 0 0 4px; }}
    .meta {{ color: #666; font-size: 0.875rem; margin-bottom: 16px; }}
    .toolbar {{
      display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
      background: #fff; padding: 12px 16px; border-radius: 8px;
      box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 16px;
    }}
    .toolbar label {{ font-size: 0.875rem; display: flex; align-items: center; gap: 6px; }}
    .toolbar input, .toolbar select {{
      padding: 6px 8px; border: 1px solid #ccc; border-radius: 4px; font-size: 0.875rem;
    }}
    .toolbar button {{
      padding: 6px 14px; border: none; border-radius: 4px; cursor: pointer; font-size: 0.875rem;
    }}
    #applyBtn {{ background: #1677ff; color: #fff; }}
    #resetBtn {{ background: #e8e8e8; color: #333; }}
    #chart {{ width: 100%; height: 520px; background: #fff; border-radius: 8px;
      box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
    .legend-hint {{ font-size: 0.8rem; color: #888; margin-top: 8px; }}
  </style>
</head>
<body>
  <h1 id="pageTitle">{escape(page_title)}</h1>
  <p class="meta" id="pageMeta">{escape(page_subtitle)}</p>
  <div class="toolbar">
    <label>指数
      <select id="indexSelect">{options_html}</select>
    </label>
    <label>横轴
      <select id="granularitySelect">
        <option value="year">年</option>
        <option value="month">月</option>
        <option value="day" selected>日</option>
      </select>
    </label>
    <label>区间
      <select id="periodSelect">
        <option value="all" selected>全量</option>
        <option value="ytd">当年</option>
        <option value="1y">近一年</option>
        <option value="3y">近三年</option>
        <option value="5y">近五年</option>
        <option value="10y">近十年</option>
        <option value="custom">自定义</option>
      </select>
    </label>
    <label>起始 <input type="date" id="startDate" value="{escape(default_filter_start)}"></label>
    <label>结束 <input type="date" id="endDate" value="{escape(default_filter_end)}"></label>
    <button type="button" id="applyBtn">筛选</button>
    <button type="button" id="resetBtn">重置</button>
  </div>
  <div id="chart"></div>
  <p class="legend-hint">蓝线：收盘价；绿点：买入信号；红点：卖出信号（如有）。横轴可选年/月/日聚合；可拖拽下方滑块缩放，或使用日期筛选。</p>
  <script>
    const ALL_DATA = {data_json};
    const PAGE_TITLE = {json.dumps(page_title, ensure_ascii=False)};
    const PAGE_SUBTITLE = {json.dumps(page_subtitle, ensure_ascii=False)};
    const DEFAULT_CODE = {json.dumps(default_code)};
    const DEFAULT_START = {json.dumps(default_filter_start)};
    const DEFAULT_END = {json.dumps(default_filter_end)};
    const DEFAULT_GRANULARITY = 'day';
    const DEFAULT_PERIOD = 'all';

    const chartEl = document.getElementById('chart');
    const chart = echarts.init(chartEl);
    const pageTitleEl = document.getElementById('pageTitle');
    const pageMetaEl = document.getElementById('pageMeta');
    const indexSelect = document.getElementById('indexSelect');
    const granularitySelect = document.getElementById('granularitySelect');
    const periodSelect = document.getElementById('periodSelect');
    const startInput = document.getElementById('startDate');
    const endInput = document.getElementById('endDate');

    indexSelect.value = DEFAULT_CODE;
    granularitySelect.value = DEFAULT_GRANULARITY;
    periodSelect.value = DEFAULT_PERIOD;

    const GRANULARITY_LABELS = {{ year: '年', month: '月', day: '日' }};
    const PERIOD_LABELS = {{
      all: '全量',
      ytd: '当年',
      '1y': '近一年',
      '3y': '近三年',
      '5y': '近五年',
      '10y': '近十年',
      custom: '自定义',
    }};

    function getSeriesStartDate(series) {{
      const dates = series.dates;
      return dates.length ? dates[0] : DEFAULT_START;
    }}

    function getSeriesEndDate(series) {{
      const dates = series.dates;
      return dates.length ? dates[dates.length - 1] : DEFAULT_END;
    }}

    function shiftYears(dateStr, years) {{
      const parts = dateStr.split('-').map(Number);
      const d = new Date(parts[0], parts[1] - 1, parts[2]);
      d.setFullYear(d.getFullYear() - years);
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      return y + '-' + m + '-' + day;
    }}

    function applyPeriod(period, series) {{
      const end = getSeriesEndDate(series);
      let start = end;
      if (period === 'ytd') {{
        start = end.slice(0, 4) + '-01-01';
      }} else if (period === '1y') {{
        start = shiftYears(end, 1);
      }} else if (period === '3y') {{
        start = shiftYears(end, 3);
      }} else if (period === '5y') {{
        start = shiftYears(end, 5);
      }} else if (period === '10y') {{
        start = shiftYears(end, 10);
      }} else if (period === 'all') {{
        start = getSeriesStartDate(series);
      }} else {{
        return;
      }}
      startInput.value = start;
      endInput.value = end;
    }}

    function expectedRange(period, series) {{
      const end = getSeriesEndDate(series);
      let start = end;
      if (period === 'ytd') start = end.slice(0, 4) + '-01-01';
      else if (period === '1y') start = shiftYears(end, 1);
      else if (period === '3y') start = shiftYears(end, 3);
      else if (period === '5y') start = shiftYears(end, 5);
      else if (period === '10y') start = shiftYears(end, 10);
      else if (period === 'all') start = getSeriesStartDate(series);
      return {{ start, end }};
    }}

    function detectPeriod(start, end, series) {{
      for (const key of ['all', 'ytd', '1y', '3y', '5y', '10y']) {{
        const exp = expectedRange(key, series);
        if (start === exp.start && end === exp.end) return key;
      }}
      return 'custom';
    }}

    function periodLabel(period) {{
      return PERIOD_LABELS[period] || '自定义';
    }}

    function describeRange(start, end, period) {{
      if (period && period !== 'custom') {{
        return periodLabel(period) + '（' + start + ' 至 ' + end + '）';
      }}
      return start + ' 至 ' + end;
    }}

    function filterSeries(series, start, end) {{
      const out = {{ dates: [], closes: [], buys: [], sells: [], buy_amounts: [] }};
      for (let i = 0; i < series.dates.length; i++) {{
        const d = series.dates[i];
        if (start && d < start) continue;
        if (end && d > end) continue;
        const c = series.closes[i];
        out.dates.push(d);
        out.closes.push(c);
        out.buys.push(!!series.buys[i]);
        out.sells.push(!!series.sells[i]);
        out.buy_amounts.push(series.buy_amounts ? (series.buy_amounts[i] || 0) : 0);
      }}
      return out;
    }}

    function formatBuyAmount(value) {{
      if (!value || value <= 0) return '';
      if (value >= 10000) return (value / 10000).toFixed(2) + ' 万元';
      return Math.round(value).toLocaleString('zh-CN') + ' 元';
    }}

    function formatSignedMoney(value) {{
      if (value == null || Number.isNaN(value)) return '—';
      const sign = value >= 0 ? '+' : '';
      return sign + Math.round(value).toLocaleString('zh-CN') + ' 元';
    }}

    function formatPct(value) {{
      if (value == null || Number.isNaN(value)) return '—';
      const sign = value >= 0 ? '+' : '';
      return sign + value.toFixed(1) + '%';
    }}

    function simulateTradeStats(filtered) {{
      let units = 0;
      let totalBought = 0;
      let totalSold = 0;
      let buyCount = 0;
      let sellCount = 0;
      let lastClose = null;
      for (let i = 0; i < filtered.dates.length; i++) {{
        const c = filtered.closes[i];
        if (c == null) continue;
        lastClose = c;
        if (filtered.buys[i]) {{
          const amt = filtered.buy_amounts[i] || 0;
          buyCount += 1;
          if (amt > 0) {{
            units += amt / c;
            totalBought += amt;
          }}
        }}
        if (filtered.sells[i] && units > 0) {{
          totalSold += units * c;
          units = 0;
          sellCount += 1;
        }}
      }}
      let profit = null;
      let returnPct = null;
      let marketValue = null;
      if (lastClose != null && totalBought > 0) {{
        marketValue = totalSold + units * lastClose;
        profit = marketValue - totalBought;
        returnPct = profit / totalBought * 100;
      }}
      return {{
        buyCount,
        sellCount,
        totalBought,
        totalSold,
        marketValue,
        profit,
        returnPct,
      }};
    }}

    function buildStatsText(stats) {{
      const parts = [];
      parts.push('买入 ' + stats.buyCount + ' 次');
      if (stats.totalBought > 0) {{
        parts.push('投入 ' + formatBuyAmount(stats.totalBought));
      }}
      if (stats.sellCount > 0) {{
        parts.push('卖出 ' + stats.sellCount + ' 次');
      }}
      if (stats.profit != null) {{
        parts.push('盈亏 ' + formatSignedMoney(stats.profit));
        parts.push('收益率 ' + formatPct(stats.returnPct));
      }}
      return parts.join(' · ');
    }}

    function updatePageHeader(series, code, start, end, period, stats) {{
      const rangeText = describeRange(start, end, period);
      const periodText = period === 'custom' ? rangeText : periodLabel(period);
      pageTitleEl.textContent = PAGE_TITLE + ' · ' + series.name + '（' + code + '）· ' + periodText;
      document.title = pageTitleEl.textContent;
      pageMetaEl.textContent = PAGE_SUBTITLE + ' · ' + rangeText + ' · ' + buildStatsText(stats);
    }}

    function periodKey(dateStr, granularity) {{
      const parts = dateStr.split('-');
      if (granularity === 'year') return parts[0];
      if (granularity === 'month') return parts[0] + '-' + parts[1];
      return dateStr;
    }}

    function formatPeriodLabel(key, granularity) {{
      if (granularity === 'year') return key + '年';
      if (granularity === 'month') {{
        const [y, m] = key.split('-');
        return y + '年' + parseInt(m, 10) + '月';
      }}
      return key;
    }}

    function aggregateSeries(filtered, granularity) {{
      if (granularity === 'day') {{
        const labels = [];
        const closes = [];
        const buyPoints = [];
        const sellPoints = [];
        let buyCount = 0;
        let sellCount = 0;
        let buyAmountTotal = 0;
        for (let i = 0; i < filtered.dates.length; i++) {{
          const d = filtered.dates[i];
          const c = filtered.closes[i];
          labels.push(d);
          closes.push(c);
          if (filtered.buys[i] && c != null) {{
            buyPoints.push([d, c]);
            buyCount += 1;
            buyAmountTotal += filtered.buy_amounts[i] || 0;
          }}
          if (filtered.sells[i] && c != null) {{
            sellPoints.push([d, c]);
            sellCount += 1;
          }}
        }}
        return {{
          labels,
          closes,
          buyPoints,
          sellPoints,
          buyCount,
          sellCount,
          buyAmountTotal,
          unit: '日',
        }};
      }}

      const order = [];
      const bucketMap = {{}};
      for (let i = 0; i < filtered.dates.length; i++) {{
        const c = filtered.closes[i];
        if (c == null) continue;
        const key = periodKey(filtered.dates[i], granularity);
        if (!bucketMap[key]) {{
          bucketMap[key] = {{
            close: c,
            hasBuy: false,
            hasSell: false,
            buyDays: 0,
            sellDays: 0,
            buyAmount: 0,
          }};
          order.push(key);
        }} else {{
          bucketMap[key].close = c;
        }}
        if (filtered.buys[i]) {{
          bucketMap[key].hasBuy = true;
          bucketMap[key].buyDays += 1;
          bucketMap[key].buyAmount += filtered.buy_amounts[i] || 0;
        }}
        if (filtered.sells[i]) {{
          bucketMap[key].hasSell = true;
          bucketMap[key].sellDays += 1;
        }}
      }}

      const labels = order.map(k => formatPeriodLabel(k, granularity));
      const closes = order.map(k => bucketMap[k].close);
      const buyPoints = [];
      const sellPoints = [];
      let buyCount = 0;
      let sellCount = 0;
      let buyAmountTotal = 0;
      order.forEach((key, idx) => {{
        const b = bucketMap[key];
        const label = labels[idx];
        const close = b.close;
        buyCount += b.buyDays;
        sellCount += b.sellDays;
        buyAmountTotal += b.buyAmount;
        if (b.hasBuy) buyPoints.push([label, close]);
        if (b.hasSell) sellPoints.push([label, close]);
      }});

      return {{
        labels,
        closes,
        buyPoints,
        sellPoints,
        buyCount,
        sellCount,
        buyAmountTotal,
        unit: GRANULARITY_LABELS[granularity],
      }};
    }}

    function render() {{
      const code = indexSelect.value;
      const series = ALL_DATA[code];
      if (!series) return;
      const start = startInput.value || null;
      const end = endInput.value || null;
      const period = periodSelect.value || DEFAULT_PERIOD;
      const granularity = granularitySelect.value || DEFAULT_GRANULARITY;
      const filtered = filterSeries(series, start, end);
      const stats = simulateTradeStats(filtered);
      const chartData = aggregateSeries(filtered, granularity);
      const unit = chartData.unit;
      const statsText = buildStatsText(stats);

      updatePageHeader(series, code, start, end, period, stats);

      chart.setOption({{
        title: {{
          text: series.name + ' (' + code + ')',
          subtext: '样本 ' + chartData.labels.length + ' ' + unit + ' · ' + statsText,
          left: 'center',
          textStyle: {{ fontSize: 15 }},
          subtextStyle: {{ fontSize: 12, color: '#888' }},
        }},
        tooltip: {{
          trigger: 'axis',
          axisPointer: {{ type: 'cross' }},
        }},
        grid: {{ left: 56, right: 24, top: 72, bottom: 72 }},
        dataZoom: [
          {{ type: 'inside', start: 0, end: 100 }},
          {{ type: 'slider', start: 0, end: 100, height: 22, bottom: 12 }},
        ],
        xAxis: {{
          type: 'category',
          data: chartData.labels,
          boundaryGap: granularity !== 'day',
          axisLabel: {{ rotate: granularity === 'day' ? 30 : 0, fontSize: 10 }},
        }},
        yAxis: {{
          type: 'value',
          scale: true,
          axisLabel: {{
            formatter: v => v >= 1000 ? (v/1000).toFixed(1) + 'k' : v,
          }},
        }},
        series: [
          {{
            name: '收盘价',
            type: 'line',
            data: chartData.closes,
            showSymbol: granularity !== 'day',
            symbolSize: granularity === 'year' ? 8 : 6,
            lineStyle: {{ width: 1.5, color: '#1677ff' }},
            areaStyle: {{ color: 'rgba(22,119,255,0.06)' }},
          }},
          {{
            name: '买入',
            type: 'scatter',
            data: chartData.buyPoints,
            symbolSize: granularity === 'day' ? 10 : 12,
            itemStyle: {{ color: '#52c41a' }},
            z: 10,
          }},
          {{
            name: '卖出',
            type: 'scatter',
            data: chartData.sellPoints,
            symbolSize: granularity === 'day' ? 10 : 12,
            symbol: 'triangle',
            itemStyle: {{ color: '#ff4d4f' }},
            z: 10,
          }},
        ],
      }}, true);
    }}

    indexSelect.addEventListener('change', () => {{
      const series = ALL_DATA[indexSelect.value];
      if (periodSelect.value !== 'custom') {{
        applyPeriod(periodSelect.value, series);
      }}
      render();
    }});
    granularitySelect.addEventListener('change', render);
    periodSelect.addEventListener('change', () => {{
      if (periodSelect.value === 'custom') return;
      applyPeriod(periodSelect.value, ALL_DATA[indexSelect.value]);
      render();
    }});
    document.getElementById('applyBtn').addEventListener('click', () => {{
      const series = ALL_DATA[indexSelect.value];
      periodSelect.value = detectPeriod(startInput.value, endInput.value, series);
      render();
    }});
    document.getElementById('resetBtn').addEventListener('click', () => {{
      granularitySelect.value = DEFAULT_GRANULARITY;
      periodSelect.value = DEFAULT_PERIOD;
      applyPeriod(DEFAULT_PERIOD, ALL_DATA[indexSelect.value]);
      render();
    }});
    window.addEventListener('resize', () => chart.resize());
    applyPeriod(DEFAULT_PERIOD, ALL_DATA[DEFAULT_CODE]);
    render();
  </script>
</body>
</html>
"""


def save_backtest_html(
    path,
    title: str,
    daily_tables: list[dict],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    subtitle: str = "",
    return_pct_by_code: dict[str, float | None] | None = None,
) -> str:
    """写入 HTML 文件并返回路径。"""
    html = render_backtest_html(
        title,
        daily_tables,
        start_date=start_date,
        end_date=end_date,
        subtitle=subtitle,
        return_pct_by_code=return_pct_by_code,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return str(path)
