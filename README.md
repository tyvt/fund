# 投资信号推送

基于估值分位、股债利差等指标，对红利指数、A 股宽基、创业板指、美股指数生成买入/观望/卖出信号，支持本地查看报告与微信定时推送。

## 环境要求

- Python 3.9+
- 依赖：`pandas`、`akshare`、`requests`

```bash
pip install pandas akshare requests
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
# 全部模块（红利 + A500 + 创业板 + 纳指100）
python report.py

# 指定模块
python report.py -m dividend
python report.py -m a500
python report.py -m hs300
python report.py -m zz500
python report.py -m zz1000
python report.py -m kc50
python report.py -m cyb
python report.py -m hstech
python report.py -m ndx

# 红利模块：只看某只指数
python report.py -m dividend --index H30269
python report.py -m dividend --index 930955 --index H30269

# 创业板：覆盖预期增速
python report.py -m cyb --growth 0.35

# 纳指100：覆盖预期盈利增速
python report.py -m ndx --ndx-growth 0.15
```

| 参数 | 说明 |
|------|------|
| `-m, --module` | 模块：`dividend` / `a500` / `hs300` / `zz500` / `zz1000` / `kc50` / `cyb` / `hstech` / `ndx` / `spx` / `all`（可多次指定，默认 all） |
| `--index` | 红利模块：指数代码，可多次指定 |
| `--growth` | 创业板：机构预期净利润增速（小数） |
| `--ndx-growth` | 纳指100：预期盈利增速（小数） |

### 推送（`push.py`）

参数与 `report.py` 相同，额外支持 `--quiet`（不打印报告正文，仅推送）。

```bash
python push.py
python push.py -m dividend
python push.py --quiet
```

定时任务通过 `run_push.bat` 调用，日志写入 `logs/push.log`。

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

回测结果自动写入 `logs/backtest/{年份}.md`（每年一份，重新运行会覆盖），包含各指数统计、收益与买入日期。

### 买卖波段回测（`backtest_trade_signals.py`）

按当前买入/卖出标准模拟波段交易（触发卖点时清仓；红利/美股仅买入持有）。

```bash
# 2021 年至今（默认）
python backtest_trade_signals.py --start 2021-01-01

# 自定义区间与每次买入金额
python backtest_trade_signals.py --start 2021-01-01 --end 2025-12-31 --amount 300
```

结果写入 `logs/backtest/trade_2021_present.md`，含买卖次数、收益对比（买卖波段 vs 仅买入持有）及全部买卖日期。

### 宽基对比（`compare_broad_indices.py`）

对比 A 股主要宽基指数基日以来收益、年化收益、最大回撤（非策略信号）。

```bash
python compare_broad_indices.py
```

---

## 文件说明

### 入口脚本

| 文件 | 作用 |
|------|------|
| `report.py` | **本地报告入口**。按模块拉取数据、评估信号、打印报告，不推送。 |
| `push.py` | **微信推送入口**。调用 `report.generate_reports()` 生成报告，经 Server酱 推送。 |
| `backtest_buy_signals.py` | 买入信号历史回测工具，统计各模块买入天数与定投收益。 |
| `backtest_trade_signals.py` | 买卖信号波段回测，对比买卖策略与仅买入持有。 |
| `compare_broad_indices.py` | A 股宽基指数收益/回撤对比工具，与策略信号无关。 |

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
| A500（薄封装） | `a500_data.py` | `a500_signal.py` | 000510 |
| 创业板 | `cyb_data.py` | `cyb_signal.py` | 399006 |
| 恒生科技 | `hstech_data.py` | `hstech_signal.py` | HSTECH |
| 纳指100 | `ndx_data.py` | `ndx_signal.py` | NDX |

- **数据层**：拉取行情/估值、构建历史面板、计算分位。
- **信号层**：按 `config.py` 阈值判定买入/观望/卖出，格式化报告段落。
- **`core.py`**：红利模块的信号与报告逻辑（`generate_report()` 供 `report.py` 调用）。

### 共享库

| 文件 | 作用 |
|------|------|
| `config.py` | 全局配置：指数列表、买卖阈值、环境变量读取、`push.env` 加载。 |
| `data_sources.py` | 数据源 URL 注册表与接口说明（中证、东财、FRED、新浪等）。 |
| `market_data.py` | 公共行情工具：国债收益率、中证历史行情、分位计算、UTF-8 输出。 |
| `signal_format.py` | 统一信号文案：买入/观望/卖出标记、判定条件块、模块标题格式。 |
| `drop_to_buy.py` | 「再跌多少可触发买入」推演（基于昨日估值面板，供盘中参考）。 |
| `notify.py` | Server酱 微信推送封装，与报告生成解耦。 |

### 配置文件

| 文件 | 作用 |
|------|------|
| `push.env` | 实际配置（含 SendKey），**勿提交版本库**。 |
| `push.example.env` | 配置模板，可复制为 `push.env`。 |

阈值默认值在 `config.py`，可通过 `push.env` 覆盖，详见 `push.example.env` 注释。

### 运行时目录

| 路径 | 作用 |
|------|------|
| `logs/push.log` | 定时推送日志。 |
| `logs/data_cache/` | A 股/创业板/恒生科技等历史数据本地缓存（按日刷新，当日已拉取则复用）。 |
| `logs/us_index_cache/` | 美股指数数据本地缓存（FRED、Forward PE、akshare 备用行情等，按日刷新）。 |

---

## 模块与策略概要

### 红利（dividend）

- **指数**：中证红利低波100（930955）、中证红利低波动（H30269）
- **买入**：股息率-国债利差 > 阈值，且利差分位高、PE 分位低
- **卖出**：无（长期持有型）

### 中证 A500（a500）

- **指数**：中证A500（000510），2024 年 9 月发布；与**中证500（000905）**为不同指数，勿混淆
- **买入**：股债利差分位达标 + PE/PB 分位达标 + 价格位置
- **卖出**：PE 分位偏高且（利差收敛或距近期低点涨幅过大），须同时满足

### A 股宽基（hs300 / zz500 / zz1000 / kc50）

- **指数**：沪深300（000300）、中证500（000905）、中证1000（000852）、科创50（000688）
- **买入**：股债利差分位达标 + PE/PB 分位达标 + 价格位置（距低点涨幅、可选距高点回撤；沪深300 要求 252 日高点回撤 ≥18%）
- **卖出**：逻辑同 A500，阈值分指数配置

### 创业板指（cyb）

- **买入**：加权 PE/PB 历史分位偏低 + PEG（近 5 年增速）≤ 阈值
- **卖出**：PE/PB 分位均偏高，或 PEG 过高且估值不低（须同时满足）

### 恒生科技指数（hstech）

- **指数**：恒生科技指数（HSTECH），2020 年 7 月发布
- **买入**：PE 分位偏低 + PEG（近 5 年增速）≤ 阈值 + 股息率分位偏高 + 价格位置（须同时满足；乐咕暂无 PB/PS 历史）
- **卖出**：PE 分位偏高，或 PEG 过高且估值不低

### 纳斯达克 100（ndx）/ 标普 500（spx）

- **买入**：Forward PE 分位偏低 + PEG(Forward) ≤ 阈值 + 10Y 利率分位不高
- **卖出**：无（长期持有型）

---

## 项目结构

```
投资推送/
├── report.py              # 本地报告入口
├── push.py                # 微信推送入口
├── core.py                # 红利信号与报告
├── dividend_data.py         # 红利数据
├── cn_broad_data.py / cn_broad_signal.py
├── a500_data.py / a500_signal.py
├── cyb_data.py / cyb_signal.py
├── hstech_data.py / hstech_signal.py
├── ndx_data.py / ndx_signal.py
├── config.py              # 配置与阈值
├── data_sources.py        # 数据源地址
├── market_data.py         # 公共行情
├── signal_format.py       # 输出格式
├── drop_to_buy.py         # 跌多少买入
├── notify.py              # 微信推送
├── backtest_buy_signals.py
├── backtest_trade_signals.py
├── compare_broad_indices.py
├── run_push.bat
├── setup_task.bat
├── push.example.env
├── push.env               # 本地配置（勿提交）
└── logs/
```

---

## 开发说明

- 调试时**仅运行** `python report.py` 查看输出，避免频繁触发微信推送。
- 修改阈值优先改 `config.py` 默认值，或通过 `push.env` 覆盖。
- 数据源变更查阅 `data_sources.py`。
