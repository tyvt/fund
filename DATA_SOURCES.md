# 外部数据来源总览

本文档汇总项目中**所有外部数据**的来源、地址与用途。URL 常量与结构化注册表以 [`data_sources.py`](data_sources.py) 为权威来源；修改接口时请同步更新该文件与本说明。

```bash
# 终端打印完整注册表
python data_sources.py
```

---

## 数据流架构

```mermaid
flowchart LR
  subgraph public [公网免费接口]
    CSIndex[中证指数]
    EM[东方财富]
    FRED[FRED]
    Sina[新浪财经]
    Legulegu[乐咕乐股]
    HoM[History of Market]
    Tencent[腾讯财经]
    Akshare[akshare 封装层]
  end
  subgraph local [本地服务]
    StockDB[StockDB :7899]
    DuckDB[(data/market.duckdb)]
    CSV[(cache/)]
  end
  public --> CSV
  StockDB --> DuckDB
  CSV --> DuckDB
  DuckDB --> Report[report.py / 回测]
  CSV --> Report
  Tencent --> Report
```

**同步入口**（见 [`sync_market_duckdb.py`](sync_market_duckdb.py)）：

| 阶段 | 脚本 | 说明 |
|------|------|------|
| 1 | `sync_data_cache.py` | 公网 → `cache/` CSV/JSON |
| 2 | `duckdb_cache.py` | CSV → DuckDB |
| 3 | `sync_stockdb_to_duckdb.py` | 本地 StockDB → DuckDB（收盘后） |

## 指数定投本地缓存

- 首次或日常联网更新运行 `python sync_data_cache.py`。同步会按数据项重试一次，继续处理其他指数，并在末尾列出失败项；有任何缺项时返回非零状态。
- 运行 `python sync_data_cache.py --check-local` 可仅检查当前缓存，不访问网络。该命令通过报告/估值使用的相同数据读取函数检查中证、国债、创业板和美股数据。
- 当 `FUND_OFFLINE=1` 时，缓存读取器只使用本地 CSV/JSON/文本缓存，不会尝试刷新过期缓存；缺失数据会报出数据键和预期路径。实时行情会跳过腾讯请求，报告回退到最近收盘价。
- 手工补充数据时，将供应商数据按相同字段整理成相同 CSV/JSON 格式，并放到错误信息提示的 `cache/` 路径；完成后重新运行 `--check-local`。不要把未覆盖的数据项当作完整同步。

---

## 一、中证指数（A 股宽基 / 策略）

| ID | 数据 | 地址 | 频率 | 调用入口 |
|----|------|------|------|----------|
| `csindex_indicator` | PE、股息率（近约 20 交易日） | `https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/indicator/{code}indicator.xls` | 日频 | `market_data.read_indicator_history` |
| `csindex_perf` | 收盘价、滚动 PE、成交额 | `https://www.csindex.com.cn/csindex-home/perf/index-perf` | 日频 | `market_data.get_index_perf_history` |
| `csindex_closeweight` | 成分股权重（预留） | `…/closeweight/{code}closeweight.xls` | 调仓日 | 未直接调用 |

**覆盖指数示例**：000688 等。

**环境变量**：`CSINDEX_INDICATOR_BASE_URL`、`INDEX_PERF_URL`

---

## 二、国债收益率（股债利差）

| ID | 数据 | 地址 | 频率 | 调用入口 |
|----|------|------|------|----------|
| `eastmoney_bond` | 中国 10Y 国债 | `https://datacenter.eastmoney.com/api/data/get`（type=RPTA_WEB_TREASURYYIELD，字段 EMM00166466） | 日频 | `market_data.get_gov_bond_yield` |
| `fred_us10y` | 美国 10Y 国债 DGS10 | `https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10` | 日频 | `us_index_data.fetch_us10y_history` |
| `sina_us_bond` | 美国 10Y（回退） | `ak.bond_zh_us_rate()` → 新浪财经 | 日频 | FRED 失败时回退 |

**说明**：中国国债历史缺口由 `config.network.BOND_YIELD_FALLBACK_BY_YEAR` 按年回填。

---

## 三、美股指数（NDX）

| ID | 数据 | 地址 | 频率 | 调用入口 |
|----|------|------|------|----------|
| `hom_ndx_forward_pe` | 纳指 100 TTM / Forward PE | `https://historyofmarket.com/api/ndx/forward-pe.json` | TTM 日频 / Forward 月频 | `us_index_data.fetch_pe_payload` |
| `fred_nasdaq100` | 纳指 100 价格指数 | `…/fredgraph.csv?id=NASDAQ100` | 日频 | `us_index_data.fetch_price_history` |
| `sina_us_ndx` | 纳指 100 日 K | `…/staticdata/us/.NDX` | 日频 | 交叉校验 / 近期补齐 |
| `nasdaq_qqq_summary` | QQQ 股息率代理 NDX | `https://api.nasdaq.com/api/quote/QQQ/summary?assetclass=etf` | 日频 | 辅助展示 |

**环境变量**：`NDX_FORWARD_PE_URL`、`FRED_CSV_BASE_URL`

**本地缓存**：`cache/us/*_forward_pe.json`、`*_price_*.csv`

### 指数定投缓存文件与字段

手工导入时使用以下文件名与字段；`--check-local` 会通过正式数据读取函数逐项校验，最终以命令列出的缺项为准。

| 文件路径 | 主要字段 |
|----------|----------|
| `cache/cn/index_perf_<code>.csv` | `date`、`close`、`rolling_pe`、`trading_value` |
| `cache/cn/indicator_<code>.csv` | `date`、`pe`、`dividend_yield`（全收益指数无此文件） |
| `cache/cyb/cyb_pe_szse.csv` | `date`、`index_close`、`pe` |
| `cache/cyb/cyb_pb.csv` | `date`、`pb`、`pb_equal`、`pb_median` |
| `cache/cyb/cyb_dividend.csv` | `date`、`dividend_yield` |
| `cache/cyb/cyb_price.csv` | `date`、`close` |
| `cache/us/ndx_forward_pe.json`、`cache/us/spx_forward_pe.json` | `current`、`trailing`、`forward`；历史记录含 `date`、`value` |
| `cache/us/ndx_price_akshare.csv`、`cache/us/spx_price_akshare.csv` | `date`、`close` |
| `cache/us/fred_NASDAQ100.csv`、`cache/us/fred_DGS10.csv` | FRED CSV 原始字段 `observation_date` 与对应序列值 |
| `cache/us/us10y_akshare.csv` | `date`、`us10y` |
| `cache/us/ndx_qqq_dividend_yield.json`、`cache/us/spx_spy_dividend_yield.json` | NASDAQ QQQ/SPY 摘要接口原始 JSON |

中证 `code` 采用六位指数代码。不得仅创建空文件；缓存需包含对应数据行。创业板与美股文件由同步入口使用的函数读取，缺项时检查命令会给出路径。

---

## 四、创业板指（399006）

| ID | 数据 | 地址 | 频率 | 调用入口 |
|----|------|------|------|----------|
| `legulegu_pe` / `legulegu_cyb_pe_page` | 创业板滚动 PE（深交所口径） | API: `https://legulegu.com/api/stock-data/market-pe`；页面: `https://legulegu.com/stockdata/cybPE` | 月度 | `cyb_data.fetch_cyb_pe_szse_official` |
| `legulegu_pb` | 创业板 PB | `https://legulegu.com/api/stockdata/index-basic-pb` | 日频 | `cyb_data.fetch_cyb_pb_history` |
| `legulegu_dividend` | 创业板股息率 | `https://legulegu.com/api/stockdata/guxilv` | 日频 | `cyb_data.fetch_cyb_dividend_history` |
| `sina_a_index` | 创业板指日 K | `https://finance.sina.com.cn/realstock/company/sz399006/hisdata/klc_kl.js` | 日频 | `ak.stock_zh_index_daily('sz399006')` |

**说明**：乐咕 PE 为月度发布；`cyb_data` 按指数收盘价折算为日度 PE。乐咕接口经 akshare 调用，需 CSRF Cookie。

---

## 五、实时行情（腾讯财经）

| ID | 数据 | 地址 | 用途 |
|----|------|------|------|
| `tencent_quote` | 指数 / 个股实时价 | `https://qt.gtimg.cn/q={代码}`（批量逗号分隔） | `realtime_quote`（000688、399006、NDX 等） |

**环境变量**：`TENCENT_QUOTE_URL`

---

## 六、本地 StockDB

| 项 | 值 |
|----|-----|
| 地址 | `127.0.0.1:7899`（TCP，非公网 HTTP） |
| SDK | `D:\repository\stockdb\pybao`（`config.paths.STOCKDB_SDK_PATH`） |
| 同步 | `python sync_stockdb_to_duckdb.py`（建议收盘后 17:30 定时任务） |
| 写入 | `data/market.duckdb`（`stock_daily`、`stock_qfq`、交易日历、股票列表） |

StockDB 本身从市场源采集数据；本项目将其作为 **A 股全市场 K 线与流动性** 的本地加速层。

---

## 七、交叉校验 / 诊断（非生产主路径）

| ID | 数据 | 地址 | 脚本 |
|----|------|------|------|
| `multpl_sp500_pe` | 标普 500 Trailing PE | `https://www.multpl.com/s-p-500-pe-ratio/table/by-month` | `scripts/data_crosscheck.py` |
| `yardeni_forward_pe` | Forward P/E 图表 | `https://yardeni.com/charts/forward-p-es/` | 人工对照 |
| `barrons_forward_pe` | Birinyi Forward PE | `https://www.barrons.com/market-data/stocks/us/pe-yields` | curl 抓取（SSL 兼容） |
| `baostock_kline` | A 股日 K | `bs.query_history_k_data_plus` | `scripts/validate_data_baostock.py` |

---

## 八、推送（非行情）

| ID | 数据 | 地址 | 用途 |
|----|------|------|------|
| `serverchan` | 微信推送 | `https://sctapi.ftqq.com/{SendKey}.send` | `notify.send_wechat` |

注册 SendKey：https://sct.ftqq.com

---

## 九、前端 CDN（回测 HTML 图表）

| 资源 | 地址 | 用途 |
|------|------|------|
| ECharts 5 | `https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js` | `backtest_html.py`、`backtest_report.py` 内嵌图表 |

---

## 环境变量速查

| 变量 | 默认值来源 |
|------|------------|
| `BOND_YIELD_URL` | 东方财富国债 API |
| `INDEX_PERF_URL` | 中证 perf API |
| `CSINDEX_INDICATOR_BASE_URL` | 中证指标 Excel |
| `TENCENT_QUOTE_URL` | 腾讯行情 |
| `FRED_CSV_BASE_URL` | FRED CSV |
| `NDX_FORWARD_PE_URL` | History of Market |
| `SERVERCHAN_API_URL` | Server 酱 |
| `STOCKDB_HOST` / `STOCKDB_PORT` | 本地 StockDB |
| `BOND_YIELD_TOKEN` | 东财国债 API 可选 token |

完整列表见 `push.example.env` 与 [`config/network.py`](config/network.py)。

---

## 维护约定

1. 新增或变更外部 URL → 更新 `data_sources.py` 常量 + `DATA_SOURCES` 注册表 + 本文件对应章节。
3. DuckDB 域与 source 标签 → 见 [`docs/duckdb.md`](docs/duckdb.md)。
