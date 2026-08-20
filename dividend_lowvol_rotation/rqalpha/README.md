# RQAlpha 迁移（Step 1）

在 **RQAlpha** 中复现 `dividend_lowvol_rotation` 核心策略，并与原生 `backtest.py` **逐日 NAV 对齐**（共有交易日 `max_daily_nav_gap = 0`）。

## 架构

```
dividend_lowvol_rotation（现有）
  ├── 选股 / 排雷 / 约束 / 调仓计划     ← bridge.py + backtest 流水线复用
  └── backtest.py                       ← 对比基准线（同 bundle 行情）

RQAlpha（本目录）
  ├── 引擎层：T+1、涨跌停、停牌、佣金、印花税
  ├── 策略层：handle_bar 调仓 + order_shares 下单
  ├── 原生台账：native_cash_ledger（现金/股数/锚点，与 backtest.cash 同口径）
  └── 对比输出：rqalpha_native_nav.csv + compare_baseline.py
```

**混合设计**：选股与再平衡计划与原生 `backtest.py` 共用 `run_screening` / `compute_rebalance_plan` / `simulate_native_rebalance`，不在 RQAlpha 里重写因子。RQAlpha 负责 **A 股执行规则**；原生台账负责 **与 backtest 一致的现金与 NAV 口径**。

### 单日流程（调仓日）

```
before_trading   → 重置当日标志；调仓日预订阅 universe
handle_bar
  1. refresh_cash_to_rebalance   自上次锚点重放派息/送股/扣税
  2. 派息日 settle               入账 → 送股 → 预扣红利税（与 backtest 顺序一致）
  3. compute_rebalance_plan      与原生同一套选股
  4. simulate_native_rebalance   整手模拟（bundle 收盘价，price_map=None）
  5. set_native_cash + 覆盖 dlv_trade_shares（来自 sim_lots，不再二次累加股数）
  6. order_shares                按模拟计划向 RQ 引擎下单
after_trading    → 写 rqalpha_native_nav.csv；roll_rebalance_anchor 保存锚点
```

非调仓日：`refresh_cash_to_rebalance(today)` 后仅处理红利税，不调仓。

## 快速开始

### 1. 环境搭建

```bat
scripts\setup_rqalpha_env.bat
```

数据包默认下载到 **`D:\rqalpha\bundle`**（约 3GB）。可覆盖：

```bat
set RQALPHA_BUNDLE_PATH=D:\rqalpha\bundle
```

手动安装：

```bash
python -m venv rqalpha_env
rqalpha_env\Scripts\activate          # Windows
pip install rqalpha pandas numpy
rqalpha download-bundle -d D:\rqalpha
rqalpha version
```

### 2. 运行回测

```bat
run_rqalpha_backtest.bat --years 10 --end 2026-08-20 --capital 100000
```

```bash
python -m dividend_lowvol_rotation.rqalpha.run_backtest \
    --years 10 --end 2026-08-20 --capital 100000
```

输出：

| 文件 | 说明 |
|------|------|
| `output/dividend_lowvol/rqalpha_result.pkl` | RQAlpha sys_analyser 结果 |
| `output/dividend_lowvol/rqalpha_native_nav.csv` | 原生口径日净值（对比用） |
| `output/dividend_lowvol/rqalpha_backtest.md` | 回测报告（Markdown，与原生 `backtest.md` 同模板） |
| `output/dividend_lowvol/rqalpha_backtest.html` | 交互图表报告（ECharts） |

报告在 RQ 跑完后自动调用原生 `run_backtest` + `backtest_report` 生成（两套引擎已对齐，内容一致）。可用 `--no-report` 跳过；`--report-only` 仅生成报告；`--report-basename backtest` 可改为与原生同名文件。

`run_backtest.py` 会通过 `data_bundle_path` 显式传入 `RQALPHA_BUNDLE_PATH`，不依赖 `~/.rqalpha/bundle`。

### 3. 与原生回测对比

```bash
python -m dividend_lowvol_rotation.rqalpha.compare_baseline \
    --years 10 --end 2026-08-20
```

输出：`output/dividend_lowvol/rqalpha_vs_native.md`

**指标口径**：在两边 **共有交易日** 上计算 CAGR/回撤/Sharpe（避免原生序列比 RQ 多几个交易日导致 CAGR 虚差）。报告会注明各自 NAV 序列长度与对齐区间。

## 已对齐的规则

| 模块 | 状态 | 说明 |
|------|------|------|
| 股息率 + 低波硬过滤 | ✅ | `run_screening` |
| 排雷 / 行业 / Beta / 市值约束 | ✅ | 与原生共用 |
| 指数式排序 + 年度调仓 | ✅ | `index_annual` |
| 股息率加权 + 单股 8% 上限 | ✅ | `target_weights_for_portfolio` |
| index_rules 调出 | ✅ | `should_sell_index_rules` |
| 波动率目标降仓 | ✅ | `resolve_position_scale` |
| 整手买卖顺序 | ✅ | `simulate_native_rebalance` → `order_shares` |
| 派息 / 送股 / 红利税 | ✅ | `native_cash_ledger` + `dividend_tax_sync` |
| 成交价 | ✅ | bundle **不复权**收盘价（`DLV_EXECUTION_AT_CLOSE=true`） |
| 佣金 + 印花税 | ✅ | `execution_costs.py` ↔ RQ `sys_transaction_cost` |
| 逐日 NAV | ✅ | 共有区间 **0 分差**（2026-08 验证） |
| 调仓日成交 | ✅ | `compare_baseline` 调仓日 signed delta 一致 |

## 验证清单

- [ ] `D:\rqalpha\bundle` 存在且可读（或 `RQALPHA_BUNDLE_PATH` 指向有效路径）
- [ ] `run_backtest` 无 bundle 报错
- [ ] `compare_baseline`：`max_daily_nav_gap: 0.00 元`
- [ ] 两边 `total_return_pct` / `cagr_pct` / `max_drawdown_pct` 一致（对齐区间）
- [ ] 调仓日成交差异节均为「成交一致」
- [ ] 日志调仓日持仓数 ≤ Top N

## 环境变量

### RQAlpha 专用

| 变量 | 默认 | 说明 |
|------|------|------|
| `DLV_RQALPHA_START` | 2018-01-01 | 回测起点（`run_backtest` 注入） |
| `DLV_RQALPHA_END` | 2025-08-01 | 回测终点 |
| `DLV_RQALPHA_CAPITAL` | `DLV_BACKTEST_INITIAL_CAPITAL` | 初始资金 |
| `DLV_RQALPHA_TOP_N` | 10 | 持仓上限 |
| `DLV_RQALPHA_REBALANCE_MODE` | index_annual | 调仓模式 |
| `DLV_RQALPHA_PREFETCH_SIZE` | 150 | 预筛池大小 |
| `RQALPHA_BUNDLE_PATH` | `D:\rqalpha\bundle` | bundle 目录（`config.py`） |

### 与原生共用（`config.py`）

| 变量 | 默认 | 说明 |
|------|------|------|
| `DLV_BACKTEST_PRICE_SOURCE` | **rqalpha** | 原生回测也读 bundle，与 RQ 同源 |
| `DLV_RQALPHA_ADJUST_TYPE` | none | bundle 不复权 |
| `DLV_EXECUTION_AT_CLOSE` | true | 收盘价成交，无滑点 |
| `DLV_BACKTEST_DIVIDEND_CASH` | true | 现金分红模式 |
| `DLV_COMMISSION_RATE` / `DLV_MIN_COMMISSION_CNY` | 万 0.854 / 5 元 | 佣金 |

其余 `DLV_*`（股息率门槛、Beta 约束等）两边共用。

## 文件说明

```
rqalpha/
  bridge.py                    # BacktestContext + compute_rebalance_plan
  dividend_lowvol_strategy.py  # RQAlpha 策略（init / handle_bar / after_trading）
  native_cash_ledger.py        # 原生现金台账：锚点重放、全精度 float
  native_rebalance.py          # 整手再平衡模拟（与 backtest 同逻辑）
  dividend_tax_sync.py         # 派息日红利税（关闭引擎 dividend_tax_enabled）
  rqalpha_bundle_prices.py     # 从 bundle 读 K 线 / 分红 / 送股 / 停牌
  execution_costs.py             # 佣金倍率、印花税、滑点说明
  execution_rules.py           # 最短持有期
  bar_price_store.py           # 可选 bar 价覆盖（当前策略 price_map=None，未启用）
  symbols_rq.py                # 代码格式 XSHG/XSHE
  run_backtest.py              # Python API 回测入口
  compare_baseline.py          # 与 backtest.py 对比 + 生成 md 报告
```

## 数据源与口径

| 维度 | 原生 `backtest.py` | RQAlpha 策略台账 | RQ 引擎 |
|------|-------------------|------------------|---------|
| 行情 | bundle 不复权（`DLV_BACKTEST_PRICE_SOURCE=rqalpha`） | 同左 `store.price_at` | bar_dict 不复权 |
| 分红入账 | `accrue_dividend_cash_on_date` | `native_cash_ledger` 重放 | sys_accounts 派息（台账独立跟踪） |
| 红利税 | 派息日预扣 | `dividend_tax_sync` | **关闭**卖出补扣 |
| 成交价 | `resolve_execution_raw_price` → 收盘价 | 同左 | 收盘价（滑点=0） |
| T+1 / 涨跌停 / 停牌 | 模拟层校验 | 同左 | ✅ 引擎 enforce |
| NAV 记录 | `nav_rows` 逐日 | `rqalpha_native_nav.csv` | `portfolio`（引擎口径，对比不用） |

**默认不复权 + 现金分红**：避免「前复权 K 线 + 现金分红」双重计入。显式实验可设 `DLV_BACKTEST_KLINE_FQ=qfq`（仅 DuckDB 路径有意义）。

## 对比报告说明

运行 `compare_baseline` 后生成 `output/dividend_lowvol/rqalpha_vs_native.md`，典型字段：

- 配置区间、Top N、初始本金、行情源
- **指标对齐区间**：共有交易日的起止（指标仅在此区间计算）
- 原生 / RQAlpha 的 `total_return_pct`、`cagr_pct`、`max_drawdown_pct`、`sharpe`
- RQ 额外：`turnover`、`final_nav`
- **差异 (RQAlpha - 原生)**：`total_return_pct` / `cagr_pct` / `max_daily_nav_gap` 等
- **调仓日成交差异**：各调仓日 signed delta（当前应为「成交一致」）

重新生成：先 `run_backtest`，再 `compare_baseline`（读取最新 `rqalpha_native_nav.csv`）。

## 常见问题

**Q: init 较慢？**

`prepare_rqalpha_context` 预加载 bundle K 线、排雷、分红索引（与原生相同）。首次需等待 bundle 读取（约 30s 量级）。

**Q: 下一步？**

Step 1 验证通过后，可进入 Step 2（Qlib 因子工程化），以本 RQAlpha + 原生对齐结果为基准。
