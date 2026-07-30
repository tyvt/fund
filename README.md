# 投资信号推送

基于估值分位、股债利差等指标，对红利指数、A 股宽基、创业板指、美股指数生成买入/观望/卖出信号，支持本地查看报告与微信定时推送。

## 环境要求

- Python 3.9+
- 依赖：`pandas`、`akshare`、`requests`

```bash
pip install pandas akshare requests beautifulsoup4 curl_cffi lxml
```

## 快速开始

### 1. 配置推送密钥（仅需微信推送时）

```bash
copy push.example.env push.env
```

编辑 `push.env`，填入 [Server酱](https://sct.ftqq.com) 的 `SERVERCHAN_SENDKEY`。

### 2. 本地查看报告

```bash
python report.py
```

### 3. 微信推送（手动测试）

```bash
python push.py
```

### 4. 注册 Windows 定时任务

右键「以管理员身份运行」`setup_task.bat`，将在工作日 14:00 自动执行 `run_push.bat` 推送微信。

---

## 常用命令

### 报告（`report.py`）

本地输出，不推送微信。

```bash
# 全部模块（红利 + A股宽基 + 创业板 + 恒生科技 + 美股）
python report.py

# 指定模块
python report.py -m dividend
python report.py -m cn_broad
python report.py -m cyb
python report.py -m hstech
python report.py -m us

# 红利模块：只看某只指数
python report.py -m dividend --index H30269
python report.py -m dividend --index 930955 --index H30269

# 创业板：覆盖预期增速
python report.py -m cyb --growth 0.35

# 美股（纳指+标普）：覆盖预期盈利增速
python report.py -m us --us-growth 0.15
```

| 参数 | 说明 |
|------|------|
| `-m, --module` | 模块：`dividend` / `cn_broad` / `cyb` / `hstech` / `us` / `all`（可多次指定，默认 all） |
| `--index` | 红利模块：指数代码，可多次指定 |
| `--growth` | 创业板：机构预期净利润增速（小数） |
| `--us-growth` | 美股：预期盈利增速（小数，纳指/标普共用） |

### 推送（`push.py`）

参数与 `report.py` 相同，额外支持 `--quiet`（不打印报告正文，仅推送）。

```bash
python push.py
python push.py -m dividend
python push.py --quiet
```

定时任务通过 `run_push.bat` 调用，日志写入 `logs/push.log`。

### 数据缓存预拉（`sync_data_cache.py`）

在开盘前预拉各指数历史数据到本地 `cache/`，可显著加快 `report.py` 首次运行速度。

```bash
python sync_data_cache.py
python sync_data_cache.py --force   # 忽略当日缓存，强制重拉
```

注册 Windows 定时任务：右键「以管理员身份运行」`setup_sync_task.bat`，将在每天 09:30（上海时区）自动执行 `run_sync_cache.bat`，日志写入 `logs/sync_cache.log`。

### 回测（`backtest_buy_signals.py`）

按当前买入标准统计历史买入天数与定投收益。

```bash
# 默认回测 2025 年
python backtest_buy_signals.py

# 指定年份
python backtest_buy_signals.py --year 2024 --year 2025

# 列出买入日期
python backtest_buy_signals.py --year 2025 --list-dates

# 每个买入信号投入 500 元（默认 300，设为 0 只统计次数）
python backtest_buy_signals.py --year 2025 --amount 500
```

回测结果自动写入 `output/backtest/{年份}.md`（每年一份，重新运行会覆盖），包含各指数统计、收益与买入日期。

### 买入金额（默认：收益最大化 + 分档）

回测默认使用 **收益最大化分指数基准金额** + **按年区间位置分档浮动**。配置在 `config.py`（`BUY_AMOUNT_BASE_BY_CODE`），可用 `push.env` 按指数覆盖（如 `NDX_BUY_AMOUNT=1200`）。

#### 各指数基准单次买入（元）

| 代码 | 指数 | 基准（元） |
|------|------|----------:|
| NDX | 纳斯达克100 | 880 |
| SPX | 标普500 | 210 |
| 399006 | 创业板指 | 118 |
| 000688 | 科创50 | 38 |
| 930955 | 红利低波100 | 28 |
| H30269 | 红利低波动 | 28 |
| 000510 | 中证A500 | 28 |
| 000300 | 沪深300 | 28 |
| 000905 | 中证500 | 28 |
| 000852 | 中证1000 | 28 |
| HSTECH | 恒生科技 | 28 |

总预算约 **31.62 万**（2016–2025 回测优化）。

#### 美股限购友好（NDX / SPX）

国内 QDII 常设单日限购。以 **全组合利润金额** 为约束（非收益率），在不明显降低总利润的前提下：

- **再放宽买入标准** → 买入次数增加
- **单次基准略低于原方案、略高于上一版** → NDX **880** 元、SPX **210** 元

| 指数 | 限购前 | 当前 | 全组合利润 |
|------|--------|------|-----------|
| NDX | 210 次 × 1054 元 | **251 次 × 880 元** | — |
| SPX | 152 次 × 242 元 | **230 次 × 210 元** | — |
| 合计 | +409,120 元 | **+408,550 元**（−570 元，−0.14%） | |

#### 分档浮动（`range_4_mild`，默认启用）

每次触发买入时，按当日 **年区间位置**（0 = 近年内低点，1 = 近年内高点）调整实际投入：

```
实际投入 = 基准单次 × 分档系数
```

| 年区间位置 | 含义 | 系数 |
|-----------|------|-----:|
| 0%–22% | 近年内低位 | 1.30× |
| 22%–38% | 偏低 | 1.10× |
| 38%–52% | 标准 | 1.00× |
| 52%–100% | 偏高 | 0.85× |

回测中对历史买入日做归一化，使总分投入接近预算；实盘按当日系数直接计算即可。

#### 报告中的展示

`report.py` / `push.py` **不在文末附配置表**；每只指数信号块内会显示当日建议金额，例如：

- 观望：`基准单次 118 元；若今日买入约 **118 元**（×1.00，年区间位置 51%）`
- 买入：`买入金额: 基准 118 元 × 1.00 = **118 元**（年区间位置 51%）`

#### 环境变量（`push.env`）

| 变量 | 默认 | 说明 |
|------|------|------|
| `BUY_AMOUNT_RETURN_MAX` | `true` | 使用收益最大化分指数金额 |
| `BUY_AMOUNT_TIER_ENABLED` | `true` | 启用价格分档 |
| `BUY_AMOUNT_TIER_SCHEME` | `range_4_mild` | 分档方案名 |
| `{代码}_BUY_AMOUNT` | 见上表 | 覆盖单只指数基准金额 |

#### 回测命令

```bash
# 默认（收益最大化 + 分档）
python backtest_buy_signals.py --year 2025
python backtest_trade_signals.py

# 禁用分档 / 切换组合模式 / 统一金额
python backtest_trade_signals.py --no-tier
python backtest_trade_signals.py --portfolio
python backtest_trade_signals.py --amount 300
```

### 组合仓位（可选，`--portfolio`）

若启用组合模式（`--portfolio`），按 50/20/10/20 权重分配（总预算约 **31.62 万**）：

| 组别 | 权重 | 指数 | 单次买入（元） |
|------|------|------|---------------|
| 核心 | 50% | 红利 930955 / H30269、A500 | 944 / 902 / 638 |
| 美股 | 20% | 纳指 NDX / 标普 SPX | 256 / 62（组内偏 NDX） |
| 科创50 | 10% | 000688（唯一保留卖出） | 155 |
| 卫星 | 20% | 创业板 / 中证1000 | 239 / 49（组内偏创业板） |

- **组合模式不买入**：沪深300、中证500、恒生科技

### 买卖波段回测（`backtest_trade_signals.py`）

按当前买入/卖出标准模拟波段交易（触发卖点时清仓；红利/美股/大部分宽基/创业板/恒科仅买入持有，**仅科创50保留卖出**）。

```bash
# 2015 年至今（默认）
python backtest_trade_signals.py

# 自定义区间与每次买入金额
python backtest_trade_signals.py --start 2015-01-01 --end 2025-12-31 --amount 300
```

结果写入 `output/backtest/trade_2015_present.md`，含买卖次数、收益对比（买卖波段 vs 仅买入持有）及全部买卖日期。

---

## 文件说明

### 入口脚本

| 文件 | 作用 |
|------|------|
| `report.py` | **本地报告入口**。按模块拉取数据、评估信号、打印报告，不推送。 |
| `push.py` | **微信推送入口**。调用 `report.generate_reports()` 生成报告，经 Server酱 推送。 |
| `backtest_buy_signals.py` | 买入信号历史回测工具，统计各模块买入天数与定投收益。 |
| `backtest_trade_signals.py` | 买卖信号波段回测，对比买卖策略与仅买入持有。 |

### 批处理

| 文件 | 作用 |
|------|------|
| `run_push.bat` | 定时任务执行脚本：调用 `push.py`，输出追加到 `logs/push.log`。 |
| `setup_task.bat` | 一键注册 Windows 计划任务（工作日 14:00），并清理旧版单指数任务。 |

### 策略模块

每个投资标的拆分为「数据层」和「信号层」：

| 模块 | 数据层 | 信号/报告层 | 覆盖指数 |
|------|--------|-------------|----------|
| 红利 | `dividend_data.py` | `core.py` | 930955、H30269 |
| A股宽基 | `cn_broad_data.py` | `cn_broad_signal.py` | 000510、000300、000905、000852、000688 |
| 创业板 | `cyb_data.py` | `cyb_signal.py` | 399006 |
| 恒生科技 | `hstech_data.py` | `hstech_signal.py` | HSTECH |
| 美股 | `us_index_data.py` | `us_index_signal.py` | NDX、SPX |

- **数据层**：拉取行情/估值、构建历史面板、计算分位。
- **信号层**：按 `config.py` 阈值判定买入/观望/卖出，格式化报告段落。
- **`core.py`**：红利模块的信号与报告逻辑（`generate_report()` 供 `report.py` 调用）。

### 共享库

| 文件 | 作用 |
|------|------|
| `config.py` | 全局配置：指数列表、买卖阈值、环境变量读取、`push.env` 加载。 |
| `data_cache.py` | 历史数据本地缓存（A 股 / 美股分目录，按日刷新）。 |
| `data_sources.py` | 数据源 URL 注册表与接口说明（中证、东财、FRED、新浪等）。 |
| `market_data.py` | 公共行情工具：国债收益率、中证历史行情、分位计算、UTF-8 输出。 |
| `signal_format.py` | 统一信号文案：买入/观望/卖出标记、判定条件块、模块标题格式。 |
| `drop_to_buy.py` | 「再跌多少可触发买入」推演（含近1年区间位置，与回测一致） |
| `price_position.py` | 近1年区间位置、距低点涨幅、距高点回撤及分层阈值计算 |
| `notify.py` | Server酱 微信推送封装，与报告生成解耦。 |

### 配置文件

| 文件 | 作用 |
|------|------|
| `push.env` | 实际配置（含 SendKey），**勿提交版本库**（已加入 `.gitignore`） |
| `push.example.env` | 配置模板，可复制为 `push.env`。 |

阈值默认值在 `config.py`，可通过 `push.env` 覆盖，详见 `push.example.env` 注释。

### 运行时目录

| 路径 | 作用 |
|------|------|
| `logs/` | **仅运行时日志**（如 `push.log`，由定时任务追加写入）。 |
| `cache/` | 外部数据本地缓存：A 股/创业板/恒生科技（`cache/cn/` 等）、美股（`cache/us/`），按日刷新。 |
| `output/backtest/` | 回测 Markdown 输出（`{年份}.md`、`trade_*.md`）。 |

---

### 价格位置指标（全模块共用）

买入侧除估值分位外，统一使用 **近 252 交易日（约 1 年）** 的价格位置过滤（见 `price_position.py`）：

| 指标 | 字段名 | 含义 |
|------|--------|------|
| 近1年区间位置 | `year_range_position` | 收盘价在近 252 日高低点区间中的位置（0=最低，1=最高）；买入须 ≤ `buy_max_year_range_pct` |
| 距低点涨幅 | `pct_above_low` | 收盘价 / 近 N 日低点 − 1（N 默认 252，见各指数 `buy_low_lookback_days`） |
| 距高点回撤 | `pct_below_high` | 1 − 收盘价 / 近 252 日高点 |

**分层规则**（`config.py` 全局默认，可通过 `push.env` 覆盖）：

- **近1年低位**（区间位置 ≤ `BUY_NEAR_YEAR_LOW_RANGE_PCT`，默认 20%）：放宽估值门槛（PE/利差/利率/PEG 等），豁免距高点回撤要求（仅当区间 ≤ `BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT`，默认 12%），并放宽距低点涨幅上限。
- **近1年中高位**（区间位置 > `BUY_MID_RANGE_POSITION_PCT`，默认 35%）：收紧距低点涨幅（上限降至 `BUY_MID_RANGE_MAX_ABOVE_LOW_PCT`，默认 2%），避免反弹途中追高。
- **硬性上限**：无论估值多便宜，区间位置超过 `buy_max_year_range_pct` 一律不买。
- **均线趋势**（`MA200` 斜率，默认 60 日变化率）：MA 斜率 ≥ `BUY_TREND_MIN_MA_SLOPE_PCT`（默认 -2.5%）视为企稳可买；若 MA 仍明显下行，仅当近1年区间 ≤ `BUY_TREND_DOWNTREND_MAX_RANGE_PCT`（默认 12%）才允许买入，避免熊市反弹途中追高。

各指数 `buy_max_year_range_pct` 示例：沪深300 **45%**、中证1000 **46%**、科创50 **40%**、恒生科技 **42%**、纳指100 **45%**、标普500 **48%**。

**分指数独立变量**：宽基通过 `get_cn_broad_signal_config(code)`（`CN_BROAD_{代码}_*` 环境变量）；红利 `DIVIDEND_{代码}_*`；创业板/恒生科技/纳指/标普为 `CYB_*` / `HSTECH_*` / `NDX_*` / `SPX_*`（含 `BUY_TREND_*`、`BUY_MID_RANGE_*` 等）。默认值见 `config.py` 中 `_CN_BROAD_PER_INDEX_DEFAULTS` 与各模块常量。

上述指标已接入：`report.py` 报告输出、`backtest_buy_signals.py` / `backtest_trade_signals.py` 回测、`drop_to_buy.py` 盘中推演。红利模块不使用 MA 趋势过滤（策略为股息率+利差，价格位置口径为 60 日）。

趋势相关环境变量（见 `push.example.env`）：`BUY_TREND_MA_DAYS`、`BUY_TREND_SLOPE_LOOKBACK_DAYS`、`BUY_TREND_MIN_MA_SLOPE_PCT`、`BUY_TREND_DOWNTREND_MAX_RANGE_PCT`。

---

## 模块与策略概要

各指数完整买卖标准如下（默认阈值见 `config.py`，可通过 `push.env` 覆盖）。**价格位置**指近 252 交易日（约 1 年）的区间位置、距低点涨幅、距高点回撤及 MA200 趋势过滤，详见上文「价格位置指标」。

### 红利（dividend）— 仅买入，无卖出

| 指数 | 代码 | 买入条件（须全部满足） |
|------|------|------------------------|
| 中证红利低波100 | 930955 | 股息率−10Y国债 **> 3.4%**；利差滚动分位 **≥ 48%**；PE 分位 **≤ 65%**；距 90 日低点涨幅 **≤ 4%**；近1年区间位置 **≤ 55%**；距 252 日高点回撤 **≥ 12%** |
| 中证红利低波动 | H30269 | 同上利差 **> 3.4%**；利差分位 **≥ 56%**；PE 分位 **≤ 60%**；距低点 **≤ 5%**；近1年区间 **≤ 55%**；高点回撤 **≥ 12%** |

- **分位窗口**：利差分位约 3 年（756 交易日）；PE 分位同窗口。
- **近1年低位放宽**：区间位置 ≤ 20% 时，利差分位门槛 −10、PE 分位上限 +12，并可豁免部分回撤要求。
- **卖出**：无（长期持有型）。
- **回测收益**：使用中证全收益指数（930955→H20955、H30269→H20269）估算，含分红再投资。

### A 股宽基（cn_broad）— 买入为主，仅科创50卖出

**买入逻辑**：股债利差分位、PE 分位、PB 分位（有数据时）等纳入评分，须 **多数指标 favorable**（`score ≥ max(3, total−1)` 且 `total ≥ 2`），且股债利差达标；同时须通过**价格位置硬过滤**（距低点、高点回撤、近1年区间、MA 趋势）。

**卖出逻辑**（**仅科创50 000688 启用**，其余宽基只买不卖）：**PE 分位偏高为前提**，再叠加以下任一：股债利差分位收敛、距近期低点涨幅过大、PB 分位偏高。

| 指数 | 代码 | 买入（分位窗口约 10 年） | 卖出 |
|------|------|--------------------------|------|
| 中证 A500 | 000510 | 利差分位 **≥ 50%**；PE **≤ 72%**；PB **≤ 68%**；距 90 日低点 **≤ 10%**；近1年区间 **≤ 58%** | 无（仅买入持有） |
| 沪深300 | 000300 | 利差 **≥ 65%**；PE **≤ 54%**；PB **≤ 58%**；距低点 **≤ 5%**；近1年区间 **≤ 36%**；高点回撤 **≥ 16%** | 无 |
| 中证500 | 000905 | 利差 **≥ 68%**；PE **≤ 54%**；PB **≤ 58%**；距低点 **≤ 5%**；近1年区间 **≤ 36%** | 无 |
| 中证1000 | 000852 | 利差 **≥ 70%**；PE **≤ 52%**；PB **≤ 56%**；距低点 **≤ 5%**；近1年区间 **≤ 34%** | 无 |
| 科创50 | 000688 | 利差 **≥ 64%**；PE **≤ 52%**；PB **≤ 58%**；距低点 **≤ 7%**；近1年区间 **≤ 38%** | PE **≥ 92%** 且（利差 **≤ 22%** 或距低点 **≥ 25%**） |

- **说明**：000510 与中证500（000905）为不同指数；A500 自 2024-09 发布，历史样本较短。
- **环境变量前缀**：`CN_BROAD_{代码}_*`（A500 另兼容 `A500_*`）。

### 创业板指（cyb）— 仅买入，无卖出

| 方向 | 条件（须全部满足） |
|------|-------------------|
| **买入** | 加权 PE 分位 **≤ 46%**；加权 PB 分位 **≤ 38%**；PEG（近 5 年增速 **16.63%**）**≤ 2.2**；距 90 日低点 **≤ 6%**；近1年区间 **≤ 42%**；距 252 日高点回撤 **≥ 18%**；MA 趋势过滤 |
| **卖出** | 无（长期持有型；`CYB_SELL_ENABLED=false`） |

- **分位窗口**：PE/PB 约 10 年（2520 交易日）。
- **环境变量前缀**：`CYB_*`。

### 恒生科技（hstech）— 仅买入，无卖出

| 方向 | 条件 |
|------|------|
| **买入**（须全部满足） | PE 分位 **≤ 38%**；PEG（近 5 年增速 **15%**）**≤ 1.6**；股息率分位 **≥ 50%**；距 252 日低点 **≤ 8%**；近1年区间 **≤ 42%**；距高点回撤 **≥ 28%**；MA 趋势过滤 |
| **卖出** | 无（长期持有型；`HSTECH_SELL_ENABLED=false`） |

- **指数**：HSTECH，2020 年 7 月发布，历史约 5 年。
- **分位窗口**：PE 约 5 年（1260 日）；股息率约 3 年（756 日）。

### 纳斯达克 100 / 标普 500（us）— 仅买入，无卖出

| 指数 | 代码 | 买入条件（须全部满足） |
|------|------|------------------------|
| 纳斯达克 100 | NDX | Forward PE 分位 **≤ 75%**（无 Forward 时退化为 TTM PE **≤ 78%**）；PEG(Forward) **≤ 1.45**（高增速时上限 +0.2）；10Y 美债利率分位 **≤ 99%**；距 90 日低点 **≤ 14%**；近1年区间 **≤ 45%**；距高点回撤 **≥ 10%**；MA 趋势过滤 |
| 标普 500 | SPX | Forward PE 分位 **≤ 78%**；PEG(Forward) **≤ 1.35**（高增速 +0.15）；利率分位 **≤ 99%**；距低点 **≤ 12%**；近1年区间 **≤ 48%**；高点回撤 **≥ 8%**；MA 趋势过滤 |

- **PEG 增速**：显式配置 → 隐含增速（trailing/forward−1）→ 历史 5 年盈利增速 → 兜底（NDX **19%**、SPX **10%**）。
- **近1年低位放宽**：PE/利率/PEG 门槛放宽，豁免距高点回撤。
- **卖出**：无（长期持有型）。
- **环境变量前缀**：`NDX_*` / `SPX_*`。

### 买卖标准速查

| 模块 | 指数数 | 买入核心 | 卖出核心 |
|------|--------|----------|----------|
| 红利 | 2 | 股息利差 + 利差/PE 分位 + 价格位置 | 无 |
| 宽基 | 5 | 股债利差分位 + PE/PB 分位 + 价格位置（评分制） | 仅科创50：PE 偏高 + 利差收敛/短期涨幅过大 |
| 创业板 | 1 | PE/PB 分位 + PEG(5年) + 价格位置 | 无 |
| 恒科 | 1 | PE + PEG + 股息率分位 + 价格位置 | 无 |
| 美股 | 2 | Forward PE + PEG + 利率分位 + 价格位置 | 无 |

---

## 项目结构

```
投资推送/
├── report.py              # 本地报告入口
├── push.py                # 微信推送入口
├── core.py                # 红利信号与报告
├── dividend_data.py
├── cn_broad_data.py / cn_broad_signal.py
├── cyb_data.py / cyb_signal.py
├── hstech_data.py / hstech_signal.py
├── us_index_data.py / us_index_signal.py
├── config.py              # 配置与阈值
├── data_cache.py          # 本地数据缓存
├── data_sources.py        # 数据源地址
├── market_data.py         # 公共行情
├── signal_format.py       # 输出格式
├── drop_to_buy.py         # 跌多少买入（含近1年价格位置）
├── price_position.py      # 价格位置指标
├── notify.py              # 微信推送
├── backtest_buy_signals.py
├── backtest_trade_signals.py
├── run_push.bat
├── setup_task.bat
├── push.example.env
├── .gitignore
├── push.env               # 本地配置（勿提交）
├── logs/                  # 运行时日志（push.log）
├── cache/                 # 外部数据缓存
└── output/backtest/       # 回测 Markdown 输出
```

---

## 开发说明

- 调试时**仅运行** `python report.py` 查看输出，避免频繁触发微信推送。
- 修改阈值优先改 `config.py` 默认值，或通过 `push.env` 覆盖。
- 数据源变更查阅 `data_sources.py`。
