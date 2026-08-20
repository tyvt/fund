# 红利低波轮动 — 数据源说明

全部使用免费公开接口 + 本地 StockDB/DuckDB。

**项目级完整清单**（含中证、美股、创业板等）：[`../DATA_SOURCES.md`](../DATA_SOURCES.md)  
**结构化注册表**：[`../data_sources.py`](../data_sources.py)（`python ../data_sources.py` 可打印）

---

## 生产主路径

| 数据 | 提供方 | 接口 / 地址 | 用途 | 缓存 |
|------|--------|-------------|------|------|
| 分红方案 | 东方财富 | `ak.stock_fhps_em(date=报告期)` | TTM 分红、动态股息率分子 | `cache/dividend_lowvol/fhps_*.csv` |
| A 股日 K（前复权 close） | DuckDB ← StockDB | `tcp://127.0.0.1:7899` | 波动率、动量、回测成交价 | `data/market.duckdb` |
| 实时股价 | 腾讯财经 | `https://qt.gtimg.cn/q=` | 动态股息率分母、买入区间 | 当日内存 |
| 10Y 国债收益率 | 东方财富 | `market_data.get_gov_bond_yield` | 动态股息率门槛 | `cache/cn/` |
| 申万一级行业 | 申万（akshare） | `index_realtime_sw` + `index_component_sw` | 行业分散 | `stock_industry_sw_l1.csv` |
| 财务摘要 / 排雷 | 东方财富 | `ak.stock_financial_abstract` | ROE、负债率、现金流质量 | `risk_hist_*.csv` |
| 利润表（回退） | 东方财富 | `ak.stock_profit_sheet_by_report_em` | 利息保障倍数 | 同上 |
| 流动性截面 | StockDB / DuckDB | 日均市值、成交额 | 对标 H30269 前 90% 保留 | `liquidity_*.csv` |

**K 线读取优先级**：DuckDB → CSV 缓存 → StockDB 直查（见 `prices.py`）。

---

## 降级 / 备用

| 数据 | 接口 | 触发条件 |
|------|------|----------|
| 证监会行业 | `bs.query_stock_industry()` | `INDUSTRY_SOURCE=csrc` 或申万拉取失败 |
| StockDB 直查 | `StockDBClient.get_data` | DuckDB 缺数据且 CSV 未覆盖 |

**文档提及但未接入代码**（可作未来兜底）：

- `ak.stock_dividend_cninfo`（巨潮分红明细）
- `ak.stock_history_dividend_detail`（个股分红历史校验）

## 交叉验证（非生产）

| 数据 | 接口 | 脚本 |
|------|------|------|
| Baostock 日 K | `bs.query_history_k_data_plus` | `scripts/validate_data_baostock.py` |
| ETF 跟踪 | `ak.fund_etf_hist_sina` | `scripts/data_crosscheck.py` |

---

## 同步与定时任务

```bash
# 公网指标 + CSV → DuckDB（早盘）
python sync_market_duckdb.py

# 仅 StockDB → DuckDB（收盘后，建议 17:30）
python sync_stockdb_to_duckdb.py
```

---

## 回测局限

- 行业映射使用**当前**申万成分，非历史时点成分。
- fhps 按**报告期批次**拉取；需配置 `DLV_FHPS_REPORT_DATES` 覆盖回测起点（年报除权通常滞后约一年，如 2016 回测需含 `20151231` 批次）。
- 回测未模拟涨跌停、停牌与滑点（**RQAlpha 路径有**）。
- 现金分红模式默认用**不复权** K 线 + 分红现金（`resolve_backtest_kline_fq()`）；勿与 `qfq` 叠加除非明确对照实验。
