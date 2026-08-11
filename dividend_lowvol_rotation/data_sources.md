# 数据源说明

本模块**不接入** EasyXT / QMT / Tushare，全部使用免费公开接口。下表对比 EasyXT 原文与本地实现。

## 已接入（免费）

| 数据 | 提供方 | 接口 | 用途 |
|------|--------|------|------|
| 分红方案（除权除息日、每股派息） | 东方财富 | `akshare.stock_fhps_em` | 动态股息率分子 |
| 分红明细（备用） | 巨潮资讯 | `akshare.stock_dividend_cninfo` | 单股校验 / 兜底 |
| A 股日 K 线 | Baostock | `query_history_k_data_plus` | 60 日对数收益、年化波动率 |
| 实时股价 | 腾讯财经 | `https://qt.gtimg.cn/q=` | 动态股息率分母、买入区间 |

本地缓存目录：`cache/dividend_lowvol/`（按日刷新行情与 K 线）。

## 未接入（需额外环境或付费）

| EasyXT 数据源 | 原因 | 影响 |
|---------------|------|------|
| **Tushare `dividend_data`** | 需积分 / Token | 已用东方财富 fhps + 巨潮 cninfo 替代 |
| **EasyXT 内置本地 DB** | 专有数据层 | 使用 `cache/dividend_lowvol/` |
| **QMT `xtdata`** | 需 QMT 在线 | 已用 Baostock + 腾讯替代 |
| **东财全市场 spot/hist** | 接口不稳定 | 腾讯行情 + Baostock K 线 |

## 已扩展数据源

| 数据 | 接口 | 用途 |
|------|------|------|
| 申万一级行业 | `akshare.index_realtime_sw` + `index_component_sw` | 行业分散（周缓存） |
| 10Y 国债收益率 | 东方财富（`market_data.get_gov_bond_yield`） | 动态股息率门槛 |
| 分红历史批次 | `akshare.stock_fhps_em`（多年报告期） | TTM 分红、回测 |
| 个股分红明细 | `akshare.stock_history_dividend_detail` | TTM 校验（备用） |

## 回测局限

- 行业映射使用**当前**申万成分，非历史时点成分。
- fhps 按**报告期批次**拉取；需配置 `DLV_FHPS_REPORT_DATES` 覆盖回测起点（年报除权通常滞后约一年，如 2016 回测需含 `20151231` 批次）。
- 回测未模拟涨跌停、停牌与滑点。
