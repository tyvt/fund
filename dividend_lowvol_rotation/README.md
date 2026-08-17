# A 股红利低波轮动

在 **A 股个股** 层面做红利低波选股与回测，**仅评估**，不接入 QMT / 券商。

**策略定位**：对标 [H30269 中证红利低波动](https://www.csindex.com.cn) 编制逻辑的个人版（**10 只**），在全市场分红股池上叠加排雷；与根目录 `report.py` 的 **H30269 指数买卖信号** 是两套独立逻辑。

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

# 约束消融（仅 3 项结构性约束）+ 参数扫描
python scripts/constraint_ablation.py --sample-windows 12
python scripts/constraint_param_sweep.py --sample-windows 12
```

Windows：`run_dividend_lowvol.bat`、`run_dividend_lowvol_backtest.bat`。

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

## 报告与验证

| 工具 | 用途 |
|------|------|
| `report.py` | 实盘目标组合（与回测相同 Top 150 预筛 + index 调样逻辑）、买入价区间、PE 分位 |
| `backtest.py` | 长周期回测 → `backtest.md/html` |
| `backtest_validate.py` | WFA、蒙特卡洛、`--benchmark H30269` 指数对比 |
| `scripts/verify_risk_lookahead.py` | 暴雷股是否在事件前被排雷标记 |
| `scripts/monthly_rolling_backtest.py` | 自 2015-01 起 **79 组** × **10 年**窗口 |

---

## 主要环境变量

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

完整列表见 `config.py`。

---

## 目录与缓存

```
dividend_lowvol_rotation/
  config.py          # 参数
  strategy.py        # 实盘流水线
  scoring.py         # 筛选、行业/Beta/市值约束
  risk_screening.py  # 排雷硬过滤
  index_retention.py # 指数式调出规则
  index_portfolio.py # 目标组合与股息率加权
  enhanced_factors.py # Beta 计算
  backtest.py / report.py
```

- 缓存：`cache/dividend_lowvol/`
- 输出：`output/dividend_lowvol/`
