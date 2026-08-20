# A 股红利低波轮动

在 **A 股个股** 层面做红利低波选股与回测，**仅评估**，不接入 QMT / 券商。

**策略定位**：对标 [H30269 中证红利低波动](https://www.csindex.com.cn) 编制逻辑的个人版（**10 只**），在全市场分红股池上叠加排雷；与根目录 `report.py` 的 **H30269 指数买卖信号** 是两套独立逻辑。

## 快速运行

```bash
# 实盘评估报告（候选池、排雷通过率、建议买入）
python -m dividend_lowvol_rotation.report --capital 800000

# 10 年回测（默认 RQAlpha bundle 行情 + 现金分红口径）
python -m dividend_lowvol_rotation.backtest --years 10 --end 2026-08-20

# 与 H30269 全收益指数对比
python -m dividend_lowvol_rotation.backtest_validate --benchmark H30269 --years 10 --end 2026-08-20

# RQAlpha 回测 + 与原生逐日 NAV 对比
run_rqalpha_backtest.bat --years 10 --end 2026-08-20 --capital 100000
python -m dividend_lowvol_rotation.rqalpha.compare_baseline --years 10 --end 2026-08-20

# 排雷未来信息验证
python scripts/verify_risk_lookahead.py

# 约束消融（仅 3 项结构性约束）+ 参数扫描
python scripts/constraint_ablation.py --sample-windows 12
python scripts/constraint_param_sweep.py --sample-windows 12
```

Windows：`run_dividend_lowvol.bat`、`run_dividend_lowvol_backtest.bat`。

**RQAlpha 迁移（Step 1）**：详见 `dividend_lowvol_rotation/rqalpha/README.md`；环境 `scripts/setup_rqalpha_env.bat` → 回测 `run_rqalpha_backtest.bat`。

---

## 策略规则（当前默认）

### 核心流程

1. **样本**：全市场有分红记录的 A 股（剔除 ST）
2. **硬过滤**：股息率门槛 + 低波上限 + **排雷硬剔除** + 绝对质量底线（ROE≥8%、负债率≤70%）
3. **排序**：**股息率 → 低波**（`index_rank_panel`）
4. **选股约束**（消融保留的 3 项）：
   - **市值分层**：大盘 ≥200 亿；中小盘持仓 ≤**40%**
   - **行业分散**：单行业 ≤20%；防御行业合计 ≤45%；前三行业 ≤50%
   - **Beta 分散**：低 Beta（≤0.68）合计 ≥**45%**；高 Beta 合计 ≤81%
5. **调仓**：每年 1 月 15 日后首个交易日；**股息率加权**，单股 ≤8%
6. **调出**（`index_rules`）：股息率 <0.5% 或排雷不达标

### 约束消融结论（2026-08，存档）

> 完整 21 场景消融报告：`output/dividend_lowvol/constraint_ablation.md`（含已删除的 17 项无效因子，**勿再引入**）

| 约束 | 关闭后 Δ年化 | 关闭后 Δ回撤 | 状态 |
|------|------------|------------|------|
| 行业分散上限 | -1.51pp | -0.91pp | **保留** |
| Beta 分散 | -1.01pp | -0.91pp | **保留** |
| 市值分层仓位上限 | -0.53pp | 0 | **保留** |
| 其余 17 项（软评分、增强因子、PB 偏好、流动性等） | 0 | 0 | **已从代码删除** |

参数调优（联合回测 +1.11pp 年化）：`DLV_MV_TIER_SMALL_MAX_WEIGHT=0.40`、`DLV_BETA_MIN_LOW_FRAC=0.45`。报告见 `compare_two_params.md`。

### 排雷（硬过滤）

| 因子 | 规则 |
|------|------|
| ROE 波动率 | 高于**行业内均值**剔除 |
| 分红连续性 | 近 **5** 年每年至少 1 次 |
| 股息支付率 | **30%~70%** |
| 资产负债率 | ≤ 行业均值 × (1+**20%**) |
| 利息保障倍数 | ≥ **3** |

---

## 原生回测（`backtest.py`）

### 行情与分红（2026-08 默认口径）

| 维度 | 默认行为 | 说明 |
|------|----------|------|
| 行情源 | **RQAlpha bundle** | `DLV_BACKTEST_PRICE_SOURCE=rqalpha`（`config.py`） |
| bundle 路径 | `D:\rqalpha\bundle` | 环境变量 `RQALPHA_BUNDLE_PATH` 可覆盖 |
| K 线复权 | **不复权** | `resolve_backtest_kline_fq()` 在 rqalpha 模式下返回 `None` |
| 分红入账 | bundle 派息日 | `use_payable_date=True`，与 RQ 引擎同源 |
| 送股/转增 | bundle | `corporate_actions.py`，调仓日/日间与 RQ 对齐 |
| 成交价 | bundle 收盘价 | `resolve_execution_raw_price`；`DLV_EXECUTION_AT_CLOSE=true` 无滑点 |
| 印花税 | 卖侧按日期 | 2023-08-28 前 0.1%，之后 0.05%（`uses_rqalpha_execution_model`） |
| 红利税 | 派息日预扣 | `dividend_tax.accrue_dividend_cash_on_date` |

切换回 DuckDB 行情（旧路径对照）：

```bash
set DLV_BACKTEST_PRICE_SOURCE=duckdb
python -m dividend_lowvol_rotation.backtest --years 10
```

### 逐日 NAV 记录

- 内部 `cash` 为**全精度 float**，仅在写入 `nav_rows` 时 `round(nav, 2)` / `round(cash, 2)`
- 调仓日 NAV = 调仓**完成后**持仓 + 现金，用 `resolve_execution_raw_price` 估值
- **调仓日间** `inter_days` 区间为 `rb_date < d < next_rb`（**不含**下一调仓日），避免在调仓日重复记一条「调仓前」净值

### 与 RQAlpha 对齐

原生 `backtest.py` 为 RQAlpha 策略的**对比基准**。在共有交易日上：

- 调仓日成交（signed delta）一致
- 逐日 NAV **0 分差**（`compare_baseline` → `output/dividend_lowvol/rqalpha_vs_native.md`）
- CAGR/回撤等指标在**共有交易日**对齐区间上计算（见 `compare_baseline.py`）

---

## 报告与验证

| 工具 | 用途 |
|------|------|
| `report.py` | 实盘目标组合（与回测相同 Top 150 预筛 + index 调样逻辑）、买入价区间、PE 分位 |
| `backtest.py` | 长周期回测 → `backtest.md/html` |
| `backtest_validate.py` | WFA、蒙特卡洛、`--benchmark H30269` 指数对比 |
| `rqalpha/compare_baseline.py` | 原生 vs RQAlpha 逐日 NAV、调仓成交、指标对比 |
| `scripts/verify_risk_lookahead.py` | 暴雷股是否在事件前被排雷标记 |
| `scripts/monthly_rolling_backtest.py` | 自 2015-01 起 **79 组** × **10 年**窗口 |

---

## 主要环境变量

### 策略 / 约束

| 变量 | 默认 | 说明 |
|------|------|------|
| `DLV_TOP_N_BUY` | **10** | 持仓上限 |
| `DLV_BACKTEST_REBALANCE_MODE` | **index_annual** | 调仓日程 |
| `DLV_SELL_MODE` | **index_rules** | 调出：股息率/排雷硬门槛 |
| `DLV_MV_TIER_CAP_ENABLED` | **true** | 市值分层仓位上限 |
| `DLV_MV_TIER_LARGE_CNY` | **20000000000** | 大盘门槛（200 亿） |
| `DLV_MV_TIER_SMALL_MAX_WEIGHT` | **0.40** | 中小盘持仓占比上限 |
| `DLV_MAX_INDUSTRY_WEIGHT` | 0.20 | 单行业上限 |
| `DLV_BETA_MIN_LOW_FRAC` | **0.45** | 低 Beta 仓位合计下限 |
| `DLV_BETA_MAX_HIGH_FRAC` | **0.81** | 高 Beta 仓位合计上限 |
| `DLV_VOL_TARGET_ENABLED` | **true** | 组合波动超目标时降仓 |
| `DLV_DYNAMIC_VOL_ENABLED` | **true** | 动态波动上限 |

### 回测行情 / 执行（与 RQAlpha 共用）

| 变量 | 默认 | 说明 |
|------|------|------|
| `DLV_BACKTEST_PRICE_SOURCE` | **rqalpha** | `rqalpha` = bundle；`duckdb` = StockDB 路径 |
| `RQALPHA_BUNDLE_PATH` | `D:\rqalpha\bundle` | bundle 目录 |
| `DLV_BACKTEST_DIVIDEND_CASH` | **true** | 现金分红模式（不复权 K + 分红入账） |
| `DLV_EXECUTION_AT_CLOSE` | **true** | 收盘价成交，无滑点 |
| `DLV_DIVIDEND_TAX_ENABLED` | **true** | 派息日预扣红利税 |
| `DLV_BACKTEST_INITIAL_CAPITAL` | 100000 | 回测初始资金 |

完整列表见 `config.py`；RQAlpha 专用变量见 `rqalpha/README.md`。

---

## 目录与缓存

```
dividend_lowvol_rotation/
  config.py              # 参数（含 BACKTEST_PRICE_SOURCE / RQALPHA_BUNDLE_PATH）
  strategy.py            # 实盘流水线
  scoring.py             # 筛选、行业/Beta/市值约束
  risk_screening.py      # 排雷硬过滤
  index_retention.py     # 指数式调出规则
  index_portfolio.py     # 目标组合与股息率加权
  enhanced_factors.py    # Beta 计算
  corporate_actions.py   # 送股/转增（与 bundle split 对齐）
  dividend_tax.py        # 红利税（派息日预扣）
  costs.py               # 佣金、印花税、成交价解析
  prices.py              # K 线（DuckDB 或 bundle，按 PRICE_SOURCE）
  backtest.py            # 原生回测
  report.py              # 实盘报告
  rqalpha/               # RQAlpha 迁移（Step 1）
  data_sources.md        # 数据接口说明（选股仍用 fhps / DuckDB 等）
```

- 缓存：`cache/dividend_lowvol/`
- 输出：`output/dividend_lowvol/`（含 `rqalpha_vs_native.md`、`rqalpha_native_nav.csv`）

行情细节与免费数据源清单见 `data_sources.md`（**回测成交价默认已切到 bundle**，与表中 DuckDB 前复权描述并存时以 `DLV_BACKTEST_PRICE_SOURCE` 为准）。
