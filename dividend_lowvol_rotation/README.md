# A 股红利低波轮动

在 **A 股个股** 层面做红利低波选股与回测，**仅评估**，不接入 QMT / 券商。

**策略定位**：对标 [H30269 中证红利低波动](https://www.csindex.com.cn) 编制逻辑的个人版（**10 只**），在全市场分红股池上叠加排雷与质量筛选；与根目录 `report.py` 的 **H30269 指数买卖信号** 是两套独立逻辑。

## 快速运行

```bash
# 实盘评估报告（候选池、排雷通过率、建议买入）
python -m dividend_lowvol_rotation.report --capital 800000

# 10 年回测（当前默认规则）
python -m dividend_lowvol_rotation.backtest --years 10 --end 2025-08-01

# 与 H30269 全收益指数对比
python -m dividend_lowvol_rotation.backtest_validate --benchmark H30269 --years 10 --end 2025-08-01

# 排雷未来信息验证
python scripts/verify_risk_lookahead.py

# 优化前后 WFA 对比（2018/2024 极端年）
python scripts/wfa_extreme_year_compare.py
```

Windows：`run_dividend_lowvol.bat`、`run_dividend_lowvol_backtest.bat`。

---

## 策略规则（当前默认）

### 与指数的差异

| 维度 | H30269 指数 | 本策略 |
|------|-------------|--------|
| 样本空间 | 中证全指成分 | **全市场有分红记录的 A 股**（剔除 ST） |
| 持仓数 | 50 | **10** |
| 入池门槛 | 指数编制规则 | **动态股息率 + 低波 + 排雷 + 质量底线**（增强项） |
| 选股排序 | 股息率 → 低波 | **可持续股息率 → 利差分位 → 低波**（增强） |
| 调出 | 硬门槛不达标才踢 | 同左（含排雷） |
| 调仓日 | 每年 12 月第二个星期五下一交易日 | **每年 1 月 15 日后首个交易日**（避开 12 月除权密集期） |
| 权重 | 股息率加权 | 同左（单股上限 **8%**） |
| 行业分散 / PB | 无 | **保留**（见下） |

### 调仓流程（每年一次）

1. **老持仓保留检查**（`index_rules`）：仅下列情况卖出  
   - 过去一年股息率 **< 0.5%**  
   - **排雷**不通过  
   - 过去一年日均市值 / 成交额不在全 A 前 **90%**（stockdb 截面，有缓存）
2. **补足至 10 只**：从合格候选中按 **可持续股息率 → 质量扣分（低优先）→ 利差分位 → 低波** 排序，结合行业上限、Beta 分散与 PB 偏好选取  
3. **股息率加权再平衡**：对目标 10 只按股息率分配权重（单股 ≤ 8%），减持超重、买入欠配  
4. **最短持有 365 天**：与年度调样一致，利于分红税 **0%** 档

> 非调仓日不交易；无排名缓冲带、无止损止盈、无波动降仓。

### 候选池与入池过滤

**候选池动态保障**（2026-08）：合格池至少 **`top_n × 1.5`**（10 只目标 → **15** 只），理想 **`top_n × 2.0`**。仅当合格数 **< pool_min** 时才逐级放宽股息率/波动率；排雷与增强因子默认**不硬剔除**，改为排序扣分，避免为凑数买入边缘股。

| 步骤 | 规则 |
|------|------|
| 分红分子 | fhps；`latest` / `ttm` / `auto` |
| 名称剔除 | ST、*ST、退市 |
| 除权冷却 | 除权后 **5** 日内不参与 |
| 基本面 | ROE ≥ **11%**、净利同比 ≥ **-10%**（硬过滤） |
| 绝对质量底线 | ROE ≥ **8%**、负债率 ≤ **70%**（放宽不可突破） |
| 排雷 | 见下表；默认**评分扣减**排序，非直接剔除 |
| 股息率 | ≥ 动态门槛（国债 + 利差，静态底 **3.09%**） |
| 波动率 | ≤ 动态上限（静态顶 **40%**；高波动市况可收紧至 **38%**） |
| 预期股息率 | 三年平均支付率 / PE，用于排名（规避高股息陷阱） |
| **盈利动量** | 近 4 季净利润环比趋势（默认扣分，非剔除） |
| **盈利稳定性** | 近 3 年净利润变异系数（默认扣分） |
| **分红覆盖率** | 经营现金流 / 分红总额（默认扣分） |
| **利差陷阱** | 利差历史分位 ≥ **92.7%** 时加重扣分 |

候选不足 **pool_min** 时：逐级放宽排雷 skip / 增强 skip / 股息率 / 波动率。**不放宽**绝对底线。合格 **≥5 只** 时不再为凑满 10 只而放宽。

### 前瞻安全（回测 as_of 约束）

以下数据在回测中严格 **`≤ 调仓日`**，避免未来函数虚高：

| 模块 | 约束 |
|------|------|
| ROE 波动率（行业中性） | 仅用 `report_year ≤ 当年` 的 ROE 序列计算 |
| 排雷快照 | `risk_snapshot_as_of` 取最近年报 |
| 增强因子 | 季度利润带 `report_date`；无日期的旧缓存回测中弃用 |
| 分红 / 预期股息 | `ex_date ≤ as_of` |
| K 线 / Beta / 利差分位 | `date ≤ as_of` |

验证：`python scripts/verify_risk_lookahead.py`（康美/康得新等暴雷股应在事件前被标记）。

### 选股排序与分散

- **排序**：可持续股息率降序 → **质量扣分升序**（排雷+增强）→ 利差分位升序 → 低波升序
- **行业上限**：单行业 ≤ **20%**；防御行业合计 ≤ **45%**；前三行业合计 ≤ **50%**
- **Beta 分散**：低 Beta（≤**0.68**）合计 ≥ **35%**；高 Beta 合计 ≤ **81%**
- **PB 偏好**：在排名前 `top×3` 候选中，优先 PB 低于行业 **50%** 分位；全市场 PE ≥ **80%** 分位时收紧至 **30%** 分位
- **权重**：股息率加权，单股 ≤ **8%**

### 排雷（`risk_screening.py`）

默认 **`SOFT_RISK_SCORING=true`**：下列因子不达标时**扣分排序**，而非直接剔除（`hard=true` 可恢复旧硬过滤，用于审计）。

| 因子 | 规则 | 硬过滤阈值（审计用） |
|------|------|----------------------|
| 经营现金流/净利润 | 偏低扣分 | ≥ **0.8** |
| ROE 波动率 | 高于**行业内均值**扣分 | 低于行业均值 |
| 分红连续性 | 不足 5 年扣分 | 近 **5** 年每年至少 1 次 |
| 股息支付率 | 偏离 30%~70% 扣分 | **30%~70%** |
| 资产负债率 | 高于行业 cap 扣分 | ≤ 行业均值 × (1+**20%**) |
| 利息保障倍数 | 偏低扣分 | ≥ **3** |

行业均值在**当期候选 panel 横截面**上计算（非全 A 宇宙）。报告输出 **各行业排雷通过率**。

### 回测模型

| 项目 | 默认 |
|------|------|
| K 线 | **前复权**（`DLV_BACKTEST_KLINE_FQ=qfq`，DuckDB 批量加载） |
| 分红 | 除权日现金分红 **税后**计入 `cash`，可再买入 |
| 佣金 | 万 **0.854**，单边最低 **5** 元 |
| 滑点 | 动态（波动 + 成交额占流动性） |
| 分红个税 | ≤1 月 **20%**；1 月~1 年 **10%**；>1 年 **0%** |
| 波动率目标 | 组合年化波动 > **20%** 时自动降仓（`VOL_TARGET`） |

---

## 回测结果（2016-08 ~ 2025-08）

> 默认：10 只 · 1 月指数调仓 · `index_rules` · 前复权 + 现金分红 · **增强因子阈值已优化** · **波动率目标 20%** · **软性评分 + 前瞻修复**

| 指标 | 数值 |
|------|------|
| **总收益率** | **+161.0%** |
| **年化收益率** | **11.3%** |
| **最大回撤** | **-19.9%**（2018） |
| 期末净值 | 260,970 元（初始 10 万） |
| 成交 | 90 笔（买 56 / 卖 34） |

**分年（当年收益）**：2018 **+1.7%** · 2022 **-0.8%** · 2024 **+29.8%**

### 优化前后对比（硬剔除 vs 当前默认）

同区间 `scripts/wfa_extreme_year_compare.py`：

| 指标 | 硬剔除（旧） | 当前默认 |
|------|-------------|----------|
| 全段收益 / 年化 | +140.9% / 10.3% | **+161.0% / 11.3%** |
| 最大回撤 | -21.0% | **-19.9%** |
| WFA 平均超额 | -2.35% | **+2.36%** |
| **2018 年** | -5.4% | **-1.5%** |
| **2024 年** | +21.7% | **+29.4%** |
| 2018 调仓日候选池 | 28（需放宽排雷） | **54（full）** |
| 2024 调仓日候选池 | 18（需放宽） | **41（full）** |

修复前瞻 + 软性评分后，候选池更厚、换手更低，极端年表现更稳。恢复硬剔除：`DLV_SOFT_RISK_SCORING_ENABLED=false DLV_SOFT_ENHANCED_SCORING_ENABLED=false`。

对比更早默认（约 +247% / -29.5%）：**波动率目标 20%** 将回撤由 -29.5% 压至约 **-21%**；进一步压至 -16% 需 `VOL_TARGET_PCT=16~18`，年化约 **6~9%**（见 `scripts/sweep_drawdown_params.py`）。

报告：`output/dividend_lowvol/backtest.md` / `backtest.html`  
因子优化：`output/dividend_lowvol/optimize_enhanced_factors.md`  
降回撤扫描：`python scripts/sweep_drawdown_params.py`  
WFA 极端年对比：`python scripts/wfa_extreme_year_compare.py`  
指数对比：`python -m dividend_lowvol_rotation.backtest_validate --benchmark H30269 --years 10 --end 2025-08-01`

### 滚动日历稳健性（固定 ~10 年窗口 × 12 组）

> 起止日期**同步按月滚动**（非固定结束日改起点），每组时长恒定 ~9.96 年，年化/回撤可直接对比。  
> 运行：`python scripts/rolling_calendar_backtest.py` → `output/dividend_lowvol/rolling_calendar.md`

| 指标 | 数值 |
|------|------|
| 年化均值 | **15.8%** |
| 年化标准差 | **1.98%**（阈值 < 4% → ✅ 全天候底仓） |
| 年化区间 | 11.2% ~ 18.2% |
| 平均最大回撤 | -23.0% · 最差 **-34.1%**（W02，含 2015 股灾后期） |

W01（2015-08 起点）年化 13.2%、maxDD -33%；W12（2016-07 起点）年化 11.2%、maxDD -19%。起点越晚越避开 2015 尾部冲击，但 12 组标准差仍 < 2%，说明策略对日历起点不敏感。

---

## 报告与验证

| 工具 | 用途 |
|------|------|
| `report.py` | 实盘建议买入、买入价区间、PE 分位、排雷行业通过率 |
| `backtest.py` | 长周期回测 → `backtest.md/html` |
| `backtest_validate.py` | WFA、蒙特卡洛、`--benchmark H30269` 指数对比 |
| `backtest_optimize.py` | 参数网格（历史实验，非当前默认） |
| `scripts/optimize_enhanced_factors.py` | 增强因子阈值网格 + 贝叶斯优化 |
| `scripts/verify_risk_lookahead.py` | 暴雷股是否在事件前被排雷标记 |
| `scripts/wfa_extreme_year_compare.py` | 硬剔除 vs 软性评分，2018/2024 候选池与 WFA |
| `scripts/sweep_drawdown_params.py` | 降回撤参数扫描 |
| `scripts/rolling_calendar_backtest.py` | 12 组固定窗口滚动日历稳健性验证 |
| `scripts/monthly_rolling_backtest.py` | 自 2015-01 起 **79 组** × **10 年**窗口（至 2021-07 起点） |

---

## 主要环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `DLV_TOP_N_BUY` | **10** | 持仓上限 |
| `DLV_TOP_N_MIN_BUY` | **5** | 最少目标；不足 10 只时不强配 |
| `DLV_CANDIDATE_POOL_MIN_RATIO` | **1.5** | 合格池下限 = top_n × 此值 |
| `DLV_CANDIDATE_POOL_TARGET_RATIO` | **2.0** | 合格池理想 = top_n × 此值 |
| `DLV_SOFT_RISK_SCORING_ENABLED` | **true** | 排雷改评分扣减（false=硬剔除） |
| `DLV_SOFT_ENHANCED_SCORING_ENABLED` | **true** | 增强因子改评分扣减 |
| `DLV_BACKTEST_REBALANCE_MODE` | **index_annual** | 调仓日程 |
| `DLV_SELL_MODE` | **index_rules** | 调出：仅硬门槛不达标 |
| `DLV_INDEX_STYLE_RANKING` | true | 股息率 → 低波排序 |
| `DLV_INDEX_DIVIDEND_WEIGHTING` | true | 股息率加权仓位 |
| `DLV_INDEX_RETENTION_MIN_DIVIDEND_YIELD_PCT` | 0.5 | 调出股息率门槛 |
| `DLV_INDEX_RETENTION_LIQUIDITY_ENABLED` | true | 市值/成交额前 90% |
| `DLV_MAX_INDUSTRY_WEIGHT` | 0.20 | 单行业上限 |
| `DLV_MAX_SINGLE_STOCK_WEIGHT` | 0.08 | 单股上限 |
| `DLV_MAX_DEFENSIVE_INDUSTRY_WEIGHT` | 0.45 | 防御行业合计上限 |
| `DLV_MAX_TOP3_INDUSTRY_WEIGHT` | 0.50 | 前三行业合计上限 |
| `DLV_VALUATION_BUY_ENABLED` | true | PB 行业分位偏好 |
| `DLV_EXPECTED_DIVIDEND_YIELD_ENABLED` | true | 预期股息率参与排名 |
| `DLV_SUSTAINABLE_DIVIDEND_ENABLED` | true | 可持续股息率排序 |
| `DLV_YIELD_SPREAD_PERCENTILE_ENABLED` | true | 利差历史分位防陷阱 |
| `DLV_YIELD_SPREAD_PERCENTILE_TRAP` | **92.7** | 分位 ≥ 此值加重扣分（2026-08 贝叶斯优化） |
| `DLV_PROFIT_MOMENTUM_FILTER_ENABLED` | true | 盈利动量入池过滤 |
| `DLV_PROFIT_MOMENTUM_MIN_QOQ_POSITIVE` | **1** | 近 4 季环比至少 N 季为正 |
| `DLV_PROFIT_STABILITY_FILTER_ENABLED` | true | 盈利稳定性过滤 |
| `DLV_MAX_PROFIT_CV` | **0.65** | 近 3 年净利润变异系数上限 |
| `DLV_DIVIDEND_COVERAGE_FILTER_ENABLED` | true | 分红现金流覆盖率 |
| `DLV_MIN_DIVIDEND_COVERAGE` | **1.27** | 经营现金流 / 分红总额下限 |
| `DLV_BETA_BALANCE_ENABLED` | true | Beta 分散约束 |
| `DLV_BETA_LOW_THRESHOLD` | **0.68** | 低 Beta 阈值 |
| `DLV_BETA_MIN_LOW_FRAC` | **0.35** | 低 Beta 仓位合计下限 |
| `DLV_BETA_MAX_HIGH_FRAC` | **0.81** | 高 Beta 仓位合计上限 |
| `DLV_VOL_TARGET_ENABLED` | **true** | 组合波动超目标时降仓 |
| `DLV_VOL_TARGET_PCT` | **20** | 目标年化波动率（%） |
| `DLV_INDEX_RULES_DAILY_RISK_ENABLED` | false | index 模式下调仓日间止损/紧急卖 |
| `DLV_MARKET_REGIME_ENABLED` | false | 波动预警分层降仓 |
| `DLV_MARKET_BREADTH_ENABLED` | false | 市场宽度降仓 |
| `DLV_INDEX_ANNUAL_REBALANCE_TIMING` | january | 1 月中旬调仓 |
| `DLV_INDEX_JANUARY_REBALANCE_DAY` | 15 | 1 月调仓截止日 |
| `DLV_BACKTEST_KLINE_FQ` | **qfq** | 前复权（回测 K 线优先 DuckDB） |
| `DLV_BACKTEST_DIVIDEND_CASH` | true | 现金分红入账 |

增强因子默认值来自 `scripts/optimize_enhanced_factors.py`（2026-08，10 年样本 2016-08~2025-08，验证段 2021 起）。完整报告见 `output/dividend_lowvol/optimize_enhanced_factors.md`。

完整列表见 `config.py`。

---

## 目录与缓存

```
dividend_lowvol_rotation/
  config.py          # 参数
  strategy.py        # 实盘流水线
  scoring.py         # 筛选、软性评分、候选池保障
  risk_screening.py  # 排雷、行业中性、前瞻 as_of
  index_retention.py # 指数式调出规则
  index_portfolio.py # 目标组合与股息率加权
  enhanced_factors.py # 可持续股息、利差分位、盈利动量、Beta
  universe_liquidity.py  # 全市场流动性截面
  backtest.py / backtest_validate.py / report.py
```

- 缓存：`cache/dividend_lowvol/`（K 线、排雷、流动性截面等）
- 输出：`output/dividend_lowvol/`
