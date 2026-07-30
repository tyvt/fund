"""回测 HTML 折线图：收盘价走势 + 买入/卖出标记，支持时间范围筛选。"""

import json
from html import escape

import pandas as pd


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


def render_backtest_html(
    title: str,
    daily_tables: list[dict],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    subtitle: str = "",
) -> str:
    """生成含交互折线图的自包含 HTML。"""
    payload = build_chart_payload(daily_tables)
    if not payload:
        return (
            "<!DOCTYPE html><html lang='zh-CN'><head>"
            "<meta charset='UTF-8'><title>无数据</title></head>"
            "<body><p>无可用图表数据</p></body></html>"
        )

    codes = list(payload.keys())
    default_code = codes[0]
    all_dates = sorted(
        {d for s in payload.values() for d in s["dates"] if d}
    )
    range_start = start_date or (all_dates[0] if all_dates else "")
    range_end = end_date or (all_dates[-1] if all_dates else "")

    data_json = json.dumps(payload, ensure_ascii=False)
    title_esc = escape(title)
    subtitle_esc = escape(subtitle)
    options_html = "\n".join(
        f'<option value="{escape(c)}">{escape(payload[c]["name"])} ({escape(c)})</option>'
        for c in codes
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_esc}</title>
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
  <h1>{title_esc}</h1>
  <p class="meta">{subtitle_esc}</p>
  <div class="toolbar">
    <label>指数
      <select id="indexSelect">{options_html}</select>
    </label>
    <label>横轴
      <select id="granularitySelect">
        <option value="year">年</option>
        <option value="month" selected>月</option>
        <option value="day">日</option>
      </select>
    </label>
    <label>起始 <input type="date" id="startDate" value="{escape(range_start)}"></label>
    <label>结束 <input type="date" id="endDate" value="{escape(range_end)}"></label>
    <button type="button" id="applyBtn">筛选</button>
    <button type="button" id="resetBtn">重置</button>
  </div>
  <div id="chart"></div>
  <p class="legend-hint">蓝线：收盘价；绿点：买入信号；红点：卖出信号（如有）。横轴可选年/月/日聚合；可拖拽下方滑块缩放，或使用日期筛选。</p>
  <script>
    const ALL_DATA = {data_json};
    const DEFAULT_CODE = {json.dumps(default_code)};
    const DEFAULT_START = {json.dumps(range_start)};
    const DEFAULT_END = {json.dumps(range_end)};
    const DEFAULT_GRANULARITY = 'month';

    const chartEl = document.getElementById('chart');
    const chart = echarts.init(chartEl);
    const indexSelect = document.getElementById('indexSelect');
    const granularitySelect = document.getElementById('granularitySelect');
    const startInput = document.getElementById('startDate');
    const endInput = document.getElementById('endDate');

    indexSelect.value = DEFAULT_CODE;
    granularitySelect.value = DEFAULT_GRANULARITY;

    const GRANULARITY_LABELS = {{ year: '年', month: '月', day: '日' }};

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
      const granularity = granularitySelect.value || DEFAULT_GRANULARITY;
      const filtered = filterSeries(series, start, end);
      const chartData = aggregateSeries(filtered, granularity);
      const buyCount = chartData.buyCount;
      const sellCount = chartData.sellCount;
      const buyAmountTotal = chartData.buyAmountTotal;
      const unit = chartData.unit;
      const buyAmountText = formatBuyAmount(buyAmountTotal);

      chart.setOption({{
        title: {{
          text: series.name + ' (' + code + ')',
          subtext: '样本 ' + chartData.labels.length + ' ' + unit + ' · 买入 ' + buyCount + ' 次'
            + (buyAmountText ? ' · ' + buyAmountText : '')
            + (sellCount ? ' · 卖出 ' + sellCount + ' 次' : ''),
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

    indexSelect.addEventListener('change', render);
    granularitySelect.addEventListener('change', render);
    document.getElementById('applyBtn').addEventListener('click', render);
    document.getElementById('resetBtn').addEventListener('click', () => {{
      startInput.value = DEFAULT_START;
      endInput.value = DEFAULT_END;
      granularitySelect.value = DEFAULT_GRANULARITY;
      render();
    }});
    window.addEventListener('resize', () => chart.resize());
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
) -> str:
    """写入 HTML 文件并返回路径。"""
    html = render_backtest_html(
        title,
        daily_tables,
        start_date=start_date,
        end_date=end_date,
        subtitle=subtitle,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return str(path)
