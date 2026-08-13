# DuckDB 市场数据文档

> 数据库文件：`data/market.duckdb`（约 12 GB）  
> 统计时间：2026-08-12  
> 时点总数：**227,670,476**

本项目将行情与缓存数据统一存入 DuckDB，与 `cache/` 目录 CSV/JSON **双写**，读取时**优先 DuckDB**，不可用时回退 CSV。

---

## 表结构

### 时序核心表（EAV 模型）

| 表名 | 说明 |
|------|------|
| `ts_series` | 时序元数据：每条记录对应一个「领域 + 实体 + 字段」 |
| `ts_point` | 时序数据点：`(series_id, trade_date, value)` |

`series_id` 格式：`{domain}:{entity_key}:{field_name}`

示例：`stock_daily:600519:close`、`cn_index_perf:H30269:rolling_pe`

### 独立表

| 表名 | 说明 |
|------|------|
| `cn_index_indicator` | 中证指数估值指标（PE、股息率等），宽表存储 |
| `sync_meta` | 各数据集同步元信息（来源、最后同步时间、行数、日期范围） |
| `kv_snapshot` | JSON 快照（股票列表、PE 序列原文、额度分配等） |

### 策略侧独立表（`cache/dividend_lowvol` 导入）

| 表名 | 说明 |
|------|------|
| `dlv_fhps` | 分红实施记录（fhps） |
| `dlv_risk_hist` | 排雷指标历史（按 code + 报告年） |
| `dlv_industry` | 申万/证监会行业分类 |
| `dlv_liquidity` | 全市场流动性截面（按调仓日） |

---

## 数据域（domain）一览

按数据量降序排列：

| domain | 实体数 | 字段/series | 数据点数 | 日期范围 | 来源 | 说明 |
|--------|--------|-------------|----------|----------|------|------|
| `stock_daily` | 7,563 | 13 字段 × 7,563 只 | 226,937,272 | 2000-01-04 ~ 2026-08-07 | StockDB | 全市场个股日 K（不复权） |
| `stock_daily_qfq` | — | close | — | — | StockDB | 前复权收盘价（回测用） |
| `cn_index_perf` | 31 | 8 字段 | 667,698 | 1990-01-01 ~ 2026-08-11 | 中证 perf API | A 股指数行情与估值 |
| `cyb_index` | 1 (399006) | 5 | 19,665 | 2010-06-01 ~ 2026-08-11 | akshare | 创业板指 OHLCV |
| `cyb_board` | 1 (399006) | 4 | 16,304 | 2009-11-02 ~ 2026-08-11 | akshare | 创业板 PB、股息率 |
| `us_index_daily` | 2 (ndx, spx) | 1 (close) | 6,561 | 2013-01-02 ~ 2026-08-11 | akshare | 美股指数收盘价 |
| `trade_calendar` | 1 (000001) | 1 (session) | 6,284 | 2000-01-04 ~ 2026-08-07 | StockDB | A 股交易日历 |
| `cn_bond_yield` | 1 (cn) | 1 (bond_yield) | 6,146 | 2002-01-04 ~ 2026-08-11 | 东方财富 | 10 年期国债收益率 |
| `fred` | 1 (DGS10) | 1 | 4,165 | 2010-01-04 ~ 2026-07-24 | FRED | 美债 10Y（已换算为小数） |
| `us10y` | 1 (us) | 1 | 4,154 | 2010-01-04 ~ 2026-08-11 | akshare | 美债 10Y |
| `us_index_pe` | 2 (ndx, spx) | 3 | 1,833 | 2010-01-01 ~ 2026-08-11 | historyofmarket | 美股前瞻/滚动 PE |
| `cyb_pe_monthly` | 1 (399006) | 2 | 394 | 2010-06-30 ~ 2026-08-11 | akshare | 创业板月度 PE |

### `cn_index_indicator` 独立表

| index_code | 行数 | 日期范围 | 字段 |
|------------|------|----------|------|
| H30269 | 30 | 2026-07-01 ~ 2026-08-11 | pe, pe2, dividend_yield, dividend_yield2 |
| 000852 | 30 | 2026-07-01 ~ 2026-08-11 | 同上 |
| 000688 | 30 | 2026-07-01 ~ 2026-08-11 | 同上 |
| 000906 | 20 | 2026-07-15 ~ 2026-08-11 | 同上 |
| 930955 | 20 | 2026-07-09 ~ 2026-08-05 | 同上 |
| 931446 | 20 | 2026-07-09 ~ 2026-08-05 | 同上 |

---

## 各域字段详情

### stock_daily（个股日 K）

每只股票的 `entity_key` 为 6 位股票代码，共 13 个字段：

| field_name | 含义 |
|------------|------|
| open / high / low / close | 开高低收 |
| pre_close | 昨收 |
| volume | 成交量 |
| amount | 成交额 |
| turnover | 换手率 |
| pe_ttm | 滚动市盈率 |
| pb | 市净率 |
| total_mv | 总市值 |
| float_mv | 流通市值 |
| is_st | 是否 ST（0/1） |

### cn_index_perf（中证指数行情）

每个 `entity_key` 为指数代码（可能带日期后缀，如 `H30269_20180101_20260727`），8 个字段：

| field_name | 含义 |
|------------|------|
| close | 收盘价 |
| rolling_pe | 滚动 PE |
| trading_value | 成交额 |
| open / high / low | 开高低 |
| change_pct | 涨跌幅 |
| trading_vol | 成交量 |

主要指数代码：

| 代码 | 名称 |
|------|------|
| H30269 | 中证红利低波动 |
| H20269 | 中证红利低波动（全收益） |
| 000852 | 中证 1000 |
| 000688 | 科创 50 |
| 000906 | 中证 800 |
| 000001 | 上证指数（perf 历史片段） |

### cyb_*（创业板）

| domain | 对应缓存文件 | 字段 |
|--------|-------------|------|
| `cyb_pe_monthly` | `cache/cyb/cyb_pe_szse.csv` | index_close, pe |
| `cyb_board` | `cache/cyb/cyb_pb.csv`、`cyb_dividend.csv` | pb, pb_equal, pb_median, dividend_yield |
| `cyb_index` | `cache/cyb/cyb_price.csv` | open, high, low, close, volume |

### us_*（美股）

| domain | 对应缓存文件 | 字段 |
|--------|-------------|------|
| `us_index_daily` | `ndx_price_akshare.csv`、`spx_price_akshare.csv` | close |
| `us10y` | `us10y_akshare.csv` | us10y |
| `fred` | `fred_DGS10.csv` | value（÷100 后存储） |
| `us_index_pe` | `ndx_forward_pe.json`、`spx_forward_pe.json` | trailing, forward, forward_own |

### cn_bond_yield

| 对应缓存 | 字段 |
|----------|------|
| `cache/cn/bond_yield_history.csv` | bond_yield |

---

## kv_snapshot（JSON 快照）

共 8 条，键名格式 `{subdir}:{filename}`：

| snapshot_key | 内容 |
|--------------|------|
| `stockdb:股票代码` | StockDB 全市场股票列表（按 0/1/3/5/6/9 分类），7,563 只 |
| `cn:bond_yield_latest.json` | 最新国债收益率 |
| `us:ndx_forward_pe.json` | 纳指 PE 时间序列原文 |
| `us:spx_forward_pe.json` | 标普 PE 时间序列原文 |
| `us:barrons_forward_pe_snapshot.json` | Barron's 前瞻 PE 快照 |
| `us:ndx_qqq_dividend_yield.json` | QQQ 股息率 |
| `us:spx_spy_dividend_yield.json` | SPY 股息率 |
| `root:position_allocation` | 买入额度分配结果 |

---

## sync_meta（同步元数据）

共 7,564 条记录：

- 1 条交易日历：`trade_calendar:000001`
- 7,563 条个股：`stock_daily:{股票代码}`，记录每只股票的同步时间、数据行数、起止日期

---

## CSV 缓存 → DuckDB 映射

`duckdb_cache.py` 负责双写/回读，映射规则：

| 缓存路径 | DuckDB 目标 |
|----------|-------------|
| `cache/cn/bond_yield_history.csv` | `cn_bond_yield:cn` |
| `cache/cn/index_perf_{code}.csv` | `cn_index_perf:{code}` |
| `cache/cn/indicator_{code}.csv` | `cn_index_indicator` 表 |
| `cache/cyb/cyb_*.csv` | `cyb_pe_monthly` / `cyb_board` / `cyb_index` |
| `cache/us/*.csv` | `us_index_daily` / `us10y` / `fred` |
| `cache/us/*.json` | `kv_snapshot` + 部分写入 `us_index_pe` |

---

## 同步与维护命令

**推荐统一入口**（公网 + 策略 + StockDB + CSV 导入）：

```bash
# 日常全量同步（与定时任务 run_sync_market_duckdb.bat 相同）
python sync_market_duckdb.py

# 强制刷新公网缓存后同步
python sync_market_duckdb.py --force

# 分阶段
python sync_market_duckdb.py --network-only    # 国债/中证/创业板/美股
python sync_market_duckdb.py --strategy-only   # 分红/行业/000300·000906 + dlv 缓存
python sync_market_duckdb.py --stockdb-only    # StockDB 不复权 + 前复权 close
python sync_market_duckdb.py --import-only     # 仅 CSV → DuckDB

# 收盘后 StockDB 增量（定时任务 17:30）
python sync_stockdb_to_duckdb.py
python sync_stockdb_to_duckdb.py --qfq-only    # 仅前复权 close
```

定时任务：

| 脚本 | 时间 | 内容 |
|------|------|------|
| `setup_sync_task.bat` | 09:30 | `sync_market_duckdb.py` 全量 |
| `setup_sync_stockdb_task.bat` | 17:30 | `sync_stockdb_to_duckdb.py` 个股增量 |

```bash
# 从 cache/ 一次性导入 CSV/JSON（含 dividend_lowvol）
python import_cache_to_duckdb.py

# 仅策略侧
python sync_strategy_to_duckdb.py

# 全量重拉 StockDB（忽略已有数据）
python sync_stockdb_to_duckdb.py --force

# 验证数据完整性
python scripts/verify_duckdb.py
```

`sync_data_cache.py` 仍可用于仅更新公网 CSV 缓存（不写入 DuckDB）。

---

## 代码读取入口

| 模块 | 用途 |
|------|------|
| `duckdb_store.py` | 底层存储：建表、写入、宽表↔长表转换 |
| `duckdb_cache.py` | CSV 缓存与 DuckDB 的双写/回读映射 |
| `duckdb_market.py` | 高层 API：个股 K 线、交易日历、股票列表、指数行情 |
| `data_cache.py` | 统一缓存层，自动优先读 DuckDB |

常用 API：

```python
from duckdb_market import (
    list_a_share_codes,      # 股票列表
    load_trade_calendar,     # 交易日历
    load_stock_kline,        # 单只股票 K 线
    batch_load_stock_klines, # 批量 K 线
    load_index_kline,        # 指数行情
)
```

---

## 常用 SQL 查询示例

```sql
-- 查看各 domain 数据量
SELECT s.domain,
       COUNT(DISTINCT s.entity_key) AS entities,
       COUNT(p.trade_date) AS points,
       MIN(p.trade_date) AS min_date,
       MAX(p.trade_date) AS max_date
FROM ts_series s
LEFT JOIN ts_point p ON s.series_id = p.series_id
GROUP BY s.domain
ORDER BY points DESC;

-- 查询单只股票收盘价
SELECT p.trade_date, p.value AS close
FROM ts_point p
JOIN ts_series s ON p.series_id = s.series_id
WHERE s.domain = 'stock_daily'
  AND s.entity_key = '600519'
  AND s.field_name = 'close'
ORDER BY p.trade_date;

-- 查询指数滚动 PE
SELECT p.trade_date, p.value AS rolling_pe
FROM ts_point p
JOIN ts_series s ON p.series_id = s.series_id
WHERE s.domain = 'cn_index_perf'
  AND s.entity_key = 'H30269'
  AND s.field_name = 'rolling_pe'
ORDER BY p.trade_date DESC
LIMIT 10;

-- 查看同步状态
SELECT dataset, source, last_sync_at, row_count, min_date, max_date
FROM sync_meta
ORDER BY last_sync_at DESC
LIMIT 20;
```

---

## 数据完整性（2026-08-12 验证）

| 检查项 | 结果 |
|--------|------|
| 个股数量 | 7,563 只 |
| 2025 年有 close 数据 | 7,560 只 |
| 交易日历 | 6,284 天 |
| 样本 600519（2024） | 242 行（与 2024 交易日一致） |
| 结论 | 数据完整，可用 |
