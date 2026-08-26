# 指数定投策略与红利低波选股

本项目以**指数定投策略**为主体：基于估值分位、股债利差、价格位置等指标，对 **6 只指数**（红利 1 + 宽基 2 + 创业板 1 + 美股 2）生成买入/观望/卖出信号，支持本地报告、微信定时推送和回测。这里的“定投”是估值与价格条件驱动的分批投入，不是固定日期无条件扣款。

仓库还包含一套独立的 **A 股红利低波个股策略**（`dividend_lowvol_rotation/`），用于选股、组合构建以及原生/RQAlpha 双引擎回测，不接入券商自动交易。两套策略共享本地数据基础设施，但信号和持仓逻辑彼此独立。

| 策略 | 标的 | 主要入口 | 定位 |
|------|------|----------|------|
| 指数定投（主体） | H30269、000852、000688、399006、NDX、SPX | `report.py` / `push.py` / `backtest.py` | 指数估值择时与分批投入 |
| 红利低波选股（子项目） | A 股个股 | `python -m dividend_lowvol_rotation.report` | Top 10 红利低波组合评估与回测 |

## 覆盖指数（6 只）

| 模块 | 代码 | 名称 | 策略类型（实盘报告） |
|------|------|------|----------|
| 红利 | H30269 | 中证红利低波动 | 仅买入 |
| 宽基 | 000852 | 中证1000 | 买入 + 移动止盈 |
| 宽基 | 000688 | 科创50 | 买入 + 分批移动止盈 |
| 创业板 | 399006 | 创业板指 | 仅买入 |
| 美股 | NDX | 纳斯达克100 | 仅买入 |
| 美股 | SPX | 标普500 | 仅买入 |

报告末尾附带**跨指数信号对比表**（按强度排序），便于横向比较。

**实盘报告 vs 回测**：`report.py` 仅输出买入/观望（宽基另含移动止盈卖点）；`backtest_trade_signals.py` 默认跑**智能轮动**（共享资金池 + 轮动门控），红利/宽基/美股在回测中可触发移动止盈（美股另含估值卖点），创业板始终仅买。详见下文「买卖波段回测」。

## 环境要求

- Python 3.10+
- 报告与同步依赖：`pandas`、`akshare`、`requests`、`beautifulsoup4`、`curl_cffi`、`lxml`
- 本地 Parquet / 因子集市依赖：`duckdb`、`pyarrow`

```bash
pip install pandas akshare requests beautifulsoup4 curl_cffi lxml duckdb pyarrow
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

## VectorBT 向量化回测与 Jupyter 工作台

这套工作台用于 **A 股红利低波个股策略**。默认快速模式读取冻结的 RQAlpha 同源订单基准，再由开源版 VectorBT 使用 `stock_daily` 行情重放组合；因此适合快速查看十年净值、持仓和对齐结果。参数研究走纯矩阵路径，最终参数仍以 RQAlpha 验证为准。它不修改 `dividend_lowvol_rotation/`，也不会写入 `data/parquet/`。

### 现在就看回测结果

在仓库根目录打开 PowerShell：

```powershell
# 运行十年对齐回测，完成后自动打开浏览器报告
.\.venv-vectorbt\Scripts\python.exe scripts\run_vectorbt_pipeline.py --mode quick --open
```

不想重新运行，只打开最近一次已经生成的报告：

```powershell
$reportDir = Get-Content output\vectorbt\LATEST.txt
$report = Join-Path (Resolve-Path output\vectorbt).Path "$reportDir/report.html"
Start-Process $report
```

HTML 首页包含净值曲线、回撤图、核心指标和期末持仓。命令行也会打印本次实际文件路径。当前已验收的十年同源结果（2016-08-19 至 2026-07-31，初始资金 10 万元）为：期末资产 **214,243.17 元**、累计收益 **114.24%**、年化收益 **8.28%**、最大回撤 **-30.13%**；正式对齐报告见 `output/vectorbt/reports/validation_report.md`。

### 首次安装或重建独立环境

VectorBT 使用独立的 `.venv-vectorbt`，不会影响现有 `rqalpha_env`：

```powershell
py -3.10 -m venv .venv-vectorbt
.\.venv-vectorbt\Scripts\python.exe -m pip install -r requirements-vectorbt.txt
```

本项目固定使用开源版 `vectorbt==0.28.5`。不要直接用系统 Python 运行工作台脚本，否则可能出现 `ModuleNotFoundError: vectorbt`。

### 输出在哪里

每次回测都创建独立时间戳目录，不覆盖历史回测；`LATEST.txt` 只记录最近报告的位置。

| 路径 | 内容 | 怎么看 |
|------|------|--------|
| `output/vectorbt/LATEST.txt` | 最近一次报告目录指针 | 用上面的 PowerShell 命令打开 |
| `output/vectorbt/reports/<run_id>/report.html` | 净值、回撤、指标、持仓交互报告 | 浏览器打开，推荐 |
| `output/vectorbt/reports/<run_id>/report.md` | 同一结果的 Markdown 摘要 | 编辑器直接查看 |
| `output/vectorbt/backtest_results/<run_id>/nav.parquet` | 每日组合资产 | Pandas / DuckDB |
| `output/vectorbt/backtest_results/<run_id>/positions.parquet` | 每日持仓权重 | Pandas / Notebook |
| `output/vectorbt/backtest_results/<run_id>/trades.parquet` | 成交明细 | Pandas / Notebook |
| `output/vectorbt/backtest_results/<run_id>/current_holdings.parquet` | 期末持仓 | Pandas / Notebook |
| `output/vectorbt/backtest_results/<run_id>/metrics.json` | 标准绩效指标 | 文本编辑器 |
| `output/vectorbt/backtest_results/<run_id>/metadata.json` | 参数、commit、配置和数据版本 | 复现实验时核对 |
| `output/vectorbt/param_scans/` | 参数扫描 Parquet/JSON | `03_Parameter_Scan.ipynb` |
| `output/vectorbt/reports/scan_benchmark.md` | 1000 组扫描耗时、峰值内存和验收结论 | Markdown 阅读器 |
| `output/vectorbt/notebooks/` | 执行后的 Notebook HTML | 浏览器打开 |
| `logs/vectorbt.log` | 回测运行日志 | 排查失败原因 |

用 Python 读取最近一次结果：

```python
from pathlib import Path
import pandas as pd

root = Path("output/vectorbt")
report_dir = (root / "LATEST.txt").read_text(encoding="utf-8").strip()
run_id = Path(report_dir).name

nav = pd.read_parquet(root / "backtest_results" / run_id / "nav.parquet")
trades = pd.read_parquet(root / "backtest_results" / run_id / "trades.parquet")
holdings = pd.read_parquet(root / "backtest_results" / run_id / "current_holdings.parquet")
display(nav.tail(), trades.tail(), holdings)
```

### Jupyter 工作台

```powershell
.\.venv-vectorbt\Scripts\python.exe -m jupyter lab
```

浏览器打开后按用途选择：

| Notebook | 用途 |
|----------|------|
| `00_Quick_Start.ipynb` | 环境检查、短区间完整规则回测、图表和一键归档 |
| `01_Data_Exploration.ipynb` | 因子覆盖率、分布、相关性和缺失模式 |
| `02_Single_Backtest.ipynb` | 十年严格对齐回测、净值/回撤/持仓热力图、报告导出 |
| `03_Parameter_Scan.ipynb` | 多参数扫描、热力图、平行坐标和最佳参数 |
| `04_Factor_Combination.ipynb` | Rank IC、ICIR、因子相关性和组合排名 |
| `05_Compare_with_RQAlpha.ipynb` | 自动执行 VectorBT 与 RQAlpha 对齐验收 |
| `templates/backtest_template.ipynb` | 新研究实验模板 |

无需进入 Jupyter，也可执行并导出 HTML：

```powershell
.\.venv-vectorbt\Scripts\python.exe -m jupyter nbconvert --to html --execute notebooks\02_Single_Backtest.ipynb --output-dir output\vectorbt\notebooks --ExecutePreprocessor.timeout=300
```

### 参数扫描与对齐验收

```powershell
# 读取 config/vectorbt/scan_params.yaml；默认 48 组十年矩阵扫描
.\.venv-vectorbt\Scripts\python.exe scripts\run_vectorbt_pipeline.py --mode scan

# 正式性能验收：固定 1000 组，记录端到端耗时和峰值内存
.\.venv-vectorbt\Scripts\python.exe scripts\benchmark_vectorbt_scan.py

# 正式十年对齐：年化差 <0.5%、最大回撤差 <1%、Top10 重合率 >80%
.\.venv-vectorbt\Scripts\python.exe scripts\verify_vectorbt_vs_rqalpha.py --years 10 --capital 100000
```

本机正式实测 1000 组十年参数扫描：零失败，数据加载 **39.41 秒**、扫描 **185.52 秒**、端到端 **224.92 秒**，峰值进程内存 **4.49 GiB**，通过“<5 分钟、<8 GB”验收；默认 48 组扫描本体约 **7.7 秒**（首次数据加载另约 40–47 秒）。十年冻结订单重放约 **2 秒**。最近一次正式对齐验收为：2,414 个共同交易日逐日净值平均绝对差 **0.0000**、年化收益差 **0.0036%**、最大回撤差 **0%**、期末 Top 10 重合率 **100%**。

扫描是快速研究口径（`alignment_mode=false`）：因子矩阵保留源单位，ROE/负债率/ROE 波动率在应用 YAML 阈值时由小数换算为百分点；`close`、`total_mv`、`is_st` 从 `stock_daily` 加载，行业取现有申万一级缓存。扫描出的最佳参数必须再生成新的 RQAlpha 基准并运行正式验收。

### 配置与两种运行口径

| 文件 | 作用 |
|------|------|
| `config/vectorbt/strategy_params.yaml` | 因子阈值、Top N、权重、约束和调仓频率 |
| `config/vectorbt/backtest_config.yaml` | 区间、资金、费用、数据源、基准和输出目录 |
| `config/vectorbt/scan_params.yaml` | 参数网格、优化指标、并行数和 Top K |

- 默认 `--mode quick`：正式验证口径。读取 `backtest_config.yaml` 指定的冻结 RQAlpha/Parquet 基准订单，VectorBT 在 `stock_daily` 上重放，并恢复同源现金流和送股变化；这是 README 中十年结果的来源。
- 自定义 `--start/--end`：重新编译现有完整规则后运行，适合开发诊断，冷启动耗时较长。例：`.\.venv-vectorbt\Scripts\python.exe scripts\run_vectorbt_pipeline.py --mode quick --start 2025-01-01 --end 2025-06-30`。
- `--compile-rules`：即使使用默认日期也强制重新编译规则；不复用冻结订单，主要用于维护者检查适配器变化。

基准文件单独保存在 `output/vectorbt/baselines/`，不会覆盖原有 RQAlpha 输出。当前正式基准为 `rqalpha_parquet_10y_20160819_20260819.pkl`；它由现有 `rqalpha_env` 生成，VectorBT 环境只做只读转换与重放。

---

## 常用命令

### 报告（`report.py`）

本地输出，不推送微信。

```bash
# 全部模块（红利 + A股宽基 + 创业板 + 美股）
python report.py

# 指定模块
python report.py -m dividend
python report.py -m cn_broad
python report.py -m cyb
python report.py -m us

# 红利模块：只看某只指数
python report.py -m dividend --index H30269

# 创业板：覆盖预期增速
python report.py -m cyb --growth 0.35

# 美股（纳指+标普）：覆盖预期盈利增速
python report.py -m us --us-growth 0.15
```

| 参数 | 说明 |
|------|------|
| `-m, --module` | 模块：`dividend` / `cn_broad` / `cyb` / `us` / `all`（可多次指定，默认 all） |
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

### 数据同步（`sync_market_duckdb.py`）

推荐统一入口：公网行情 → CSV 缓存 → StockDB → DuckDB。

```bash
python sync_market_duckdb.py
python sync_market_duckdb.py --force      # 强制刷新公网缓存
python sync_market_duckdb.py --import-only  # 仅将 cache/ 导入 DuckDB
```

仅更新公网 CSV 缓存（不写入 DuckDB）时仍可用：

```bash
python sync_data_cache.py
python sync_data_cache.py --force
```

注册 Windows 定时任务：右键「以管理员身份运行」`setup_sync_task.bat`，将在每天 09:30（上海时区）自动执行 `run_sync_market_duckdb.bat`，日志写入 `logs/sync_market_duckdb.log`。

### 横截面因子宽表

宽表层只读 `data/parquet/factors/` 下的因子集市，将六个长表因子合并为按交易日分区的 Parquet：

```text
data/parquet/factors/snapshots/
├── manifest.json
└── trade_date=YYYY-MM-DD/
    └── factors.parquet
```

每个文件包含 `symbol`、`dividend_yield`、`volatility_60d`、`beta_300`、`roe`、`debt_ratio`、`roe_volatility`；因子空值原样保留。生成物位于 `.gitignore` 已忽略的 `data/` 下。

```bash
# 首次全量构建
python scripts/factor_snapshot_builder.py --full

# 日常增量同步；无变化时不重写文件
python scripts/factor_snapshot_builder.py --incremental

# 指定日期范围
python scripts/factor_snapshot_builder.py --start 2024-01-01 --end 2024-12-31

# 只更新指定因子列，已有其他因子列会保留
python scripts/factor_snapshot_builder.py --factors dividend_yield,volatility_60d

# 强制重写或从匹配的年度检查点恢复
python scripts/factor_snapshot_builder.py --full --force
python scripts/factor_snapshot_builder.py --full --resume

# 使用独立输出目录
python scripts/factor_snapshot_builder.py --full --output data/parquet/factors/snapshots_alt
```

增量同步按“因子 + 年度分区”记录源文件指纹。某年度源 Parquet 新增、删除或变化时，重建该年度全部交易日；新增因子也会补算其全部年度。`--factors` 是列级更新，不会删除 Snapshot 中未选中的已有因子列。

查询接口可从仓库根目录直接导入：

```python
from factor_snapshot_loader import load_snapshot, load_snapshots

one_day = load_snapshot("2024-12-31")
history = load_snapshots(start="2020-01-01", end="2024-12-31")
stocks = load_snapshots(
    symbols=["600519", "000858"],
    start="2020-01-01",
)
```

诊断与验收：

```bash
python scripts/factor_snapshot_inspector.py --date-range
python scripts/factor_snapshot_inspector.py --coverage 2024-12-31
python scripts/factor_snapshot_inspector.py --stats 2024-12-31
python scripts/factor_snapshot_inspector.py --status
python scripts/verify_snapshots.py --full
```

完整验收报告写入 `output/snapshot_validation_report.md`。当前本地数据实测（2026-08-25）：6 个因子、6,453 个交易日、18,739,461 行，覆盖 2000-01-04 至 2026-08-19；Snapshot 约 438 MiB。全量构建 23.72 秒，单日查询 0.058 秒，2020–2024 查询 2.658 秒，均通过验收目标。具体耗时会随磁盘、CPU 和本地数据规模变化。

### 回测（`backtest.py`）

统一入口 `backtest.py --mode <模式>`；底层实现仍保留在 `backtest_*.py` 模块中。两套主回测**均按买入信号日触发**（非每日定投）。**默认不设买入冷却期**（`BUY_COOLDOWN_ENABLED=false`）：买入频次由各指数硬门槛自然决定，再通过**位置分配**与总投入控制仓位。

| 模式 | 输出 | 区间默认 | 含义 |
|------|------|----------|------|
| `buy` | `inception_present.md/html` | 各指数**基日**→最新 | 仅验证买入择时：信号日累加买入并持有，**不含止盈卖出** |
| `trade` | `trade_inception_present.md/html` | **2015-01-01**→最新 | **智能轮动**（默认）：共享资金池 + 轮动门控卖点；对比仅买入持有 |
| `rotation` | `rotation_compare.md` | **2015-01-01**→最新 | 组合级对比：全持有 / 孤立卖出 / 池内卖出 / 智能轮动 |
| `regime` | `regime_compare.md` | **2015-01-01**→最新 | 牛熊状态开关对比（`MARKET_REGIME_ENABLED`） |
| `wfa` | `wfa_rotation.md` | 滚动窗口 | 轮动策略样本外（WFA）稳健性检验 |
| `optimize` | `optimize_*.md` | 可配置 | 阈值/仓位参数搜索（含 WFA 精修） |
| `inception` | 上述 buy + trade | — | 一键依次运行 `buy` 与 `trade` |

对比两套报告时请对齐起始日，例如：

```bash
python backtest.py -m buy --start 2015-01-01
python backtest.py -m trade --start 2015-01-01
```

对仅买入指数（红利/创业板/美股报告层），在**相同区间、相同金额模式**下，买入次数应与 `buy` 模式一致。

```bash
# 买入信号全量回测（默认：位置分配 + 年度预算 + 涨跌缩放）
python backtest.py -m buy

# 列出买入日期 / 统一金额覆盖 / 收益率排名（对比用）/ 禁用涨跌缩放
python backtest.py -m buy --list-dates
python backtest.py -m buy --amount 500
python backtest.py -m buy --ranking
python backtest.py -m buy --no-tier

# 买卖波段回测（默认智能轮动）
python backtest.py -m trade

# 组合级轮动对比 / 牛熊开关对比
python backtest.py -m rotation
python backtest.py -m regime

# 一键全量（买入 + 买卖波段）
python backtest.py -m inception
```

**指标/阈值调整后**：须跑自基日全量回测并重新生成 HTML，执行 `python backtest.py -m inception`（或分别运行 `buy` 与 `trade` 模式）。输出 `inception_present.html` 与 `trade_inception_present.html`。

各指数触发频率不同，长期持有后仓位会漂移；请按主观目标**手动再平衡**。
**历史信号频率参考**（`inception_present.md` 默认模式；数据截至最近一次全量回测，阈值变更后须重跑）：

| 指数 | 代码 | 样本（交易日） | 买入次 | 占比 | 频率说明 |
|------|------|---------------:|-------:|-----:|----------|
| 红利 | H30269 | 3,084 | 379 | 12.3% | 约每 8 个交易日 1 次 |
| 中证1000 | 000852 | 2,874 | 231 | 8.0% | 约每 12 个交易日 1 次 |
| 科创50 | 000688 | 1,467 | 212 | 14.5% | 约每 7 个交易日 1 次 |
| 创业板 | 399006 | 3,913 | 303 | 7.7% | 约每 13 个交易日 1 次 |
| 纳指100 | NDX | 4,175 | 614 | 14.7% | 约每 7 个交易日 1 次 |
| 标普500 | SPX | 3,421 | 423 | 12.4% | 约每 8 个交易日 1 次 |

取消冷却后，硬门槛连续达标日会连买。控制总投入靠**位置分配基准 + 年度预算 + 当日涨跌缩放**；纳指/标普实盘常受 QDII 每日限购约束。

### 买入金额

#### 默认：位置分配（实盘 + 回测）

默认启用 `BUY_AMOUNT_POSITION_ALLOC_ENABLED=true`（可用 `push.env` 关闭，退回固定基准 100）：

- **实盘**（`buy_amount_allocation.py`）：按各指数**当前区间位置**（越低权重越高）、**买入就绪度**（强度分或条件达标比例）、**历史策略收益**加权，在**当年剩余可用额度**内分配单次买入金额；结果按日缓存至 `cache/position_allocation.json`。
- **回测**（`compute_backtest_position_allocation`）：按各指数历史买入日的**平均区间位置**与买入频次分配固定基准（**无未来函数**），再叠加年度预算缩放与涨跌系数。

各模块报告中的「买入金额」展示分配后的基准 × 当日涨跌缩放（仅买入信号时出现）。

#### 公式（回测与实盘一致）

```
实际买入 = 基准金额 × 年度预算缩放 × clamp(1 − 敏感度 × 当日涨跌幅, 下限, 上限)
```

- **年度预算缩放**：`当年预计/配置全年投入 ÷ BUY_AMOUNT_REFERENCE_ANNUAL_BUDGET`（默认参考 **72,577** 元）；历史年份可用 `ANNUAL_INVESTMENT_BUDGET_{年份}` 覆盖
- 默认敏感度 **10**：跌 3% → 1.30×；涨 3% → 0.70×
- 系数限制在 **0.5×–2.0×**
- 无昨收/涨跌数据时按 1.0×（等于基准）

#### 回测 CLI

| 模式 | 说明 |
|------|------|
| 默认 | 位置分配 + 年度预算 + 涨跌缩放；**无未来函数** |
| `--ranking` | 按全历史收益率排名分配基准；**有未来函数**，仅供对比 |
| `--amount N` | 所有指数统一基准金额 |
| `--no-tier` | 禁用涨跌缩放，固定基准 |
| `BUY_AMOUNT_POSITION_ALLOC_ENABLED=false` | 退回各指数固定基准 100 |

#### 各指数基准单次买入（元，位置分配默认预算下）

| 代码 | 指数 | 默认基准 |
|------|------|--------:|
| 000688 | 科创50 | **97** |
| 000852 | 中证1000 | **95** |
| 399006 | 创业板指 | **83** |
| H30269 | 红利低波 | **61** |
| SPX | 标普500 | **44** |
| NDX | 纳指100 | **29** |

金额随区间位置、就绪度与剩余额度每日变化；上表为最近一次全量回测的参考值。美股 QDII 若日限约 100 元，单日两市合计可能触及限购；回测按信号全额计入，实盘请自行控制。

#### 环境变量（`push.env`）

| 变量 | 默认 | 说明 |
|------|------|------|
| `REMAINING_INVESTMENT_BUDGET` | `50000` | 当年剩余可用额度（实盘展示与分配） |
| `ANNUAL_INVESTMENT_BUDGET_ENABLED` | `true` | 回测按年度预算缩放基准 |
| `BUY_AMOUNT_REFERENCE_ANNUAL_BUDGET` | `72577` | 年度预算缩放参考值 |
| `ANNUAL_INVESTMENT_TARGET` | `0` | 指定全年投入目标（>0 时优先于外推） |
| `ANNUAL_INVESTMENT_BUDGET_{年份}` | — | 按年覆盖全年额度 |
| `BUY_AMOUNT_POSITION_ALLOC_ENABLED` | `true` | 启用位置分配 |
| `BUY_AMOUNT_DEFAULT` | `100` | 关闭位置分配时各指数默认基准 |
| `{代码}_BUY_AMOUNT` | `100` | 覆盖单只指数基准（关闭位置分配时） |
| `BUY_AMOUNT_CHANGE_SCALE_ENABLED` | `true` | 启用涨跌缩放 |
| `BUY_AMOUNT_CHANGE_SENSITIVITY` | `10` | 敏感度 |
| `BUY_AMOUNT_CHANGE_MIN_MULT` / `MAX_MULT` | `0.5` / `2.0` | 系数上下限 |

#### 轮动与牛熊（`push.env`）

| 变量 | 默认 | 说明 |
|------|------|------|
| `ROTATION_SELL_ENABLED` | `true` | 回测启用智能轮动（共享资金池 + 门控） |
| `ROTATION_MARGINAL_HURDLE_ANN_PCT` | `10` | 估值卖出门槛：持仓年化浮盈低于此值（%）才卖 |
| `MARKET_REGIME_ENABLED` | `false` | 牛熊市场状态调节买入/轮动门槛 |
| `MARKET_REGIME_PROXY_CODES` | `000852,399006,NDX` | 牛熊判定代理指数 |
| `DIVIDEND_SELL_ENABLED` / `US_INDEX_SELL_ENABLED` | `true` | 回测中红利/美股是否启用卖点 |

### 买卖波段回测（智能轮动）

`backtest_trade_signals.py` 默认输出**智能轮动**策略（`ROTATION_SELL_ENABLED=true`）：

- **共享资金池**：卖出释放的资金优先用于同日其他指数的买入，减少闲置现金
- **轮动门控**（`rotation_sell.py`）：仅当**同日有其他指数触发买入**时才允许卖出；估值类卖点还需持仓年化浮盈 **< `ROTATION_MARGINAL_HURDLE_ANN_PCT`**（默认 **10%**），避免「卖早了、钱闲着」
- **移动止盈**：宽基、红利、美股在回测中启用（创业板代码层仅买，不参与卖点）
- **估值卖点**：仅美股（Forward PE 分位高等条件）；宽基估值卖点已移除

默认区间自 **2015-01-01**（可用 `--start` 覆盖）。报告同时对比**智能轮动 vs 全持有**的组合收益、XIRR 与资金占用。

```bash
python backtest_trade_signals.py
python backtest_rotation.py          # 四种组合模式横向对比
python backtest_regime_compare.py    # 牛熊状态开关对比
```

结果写入 `output/backtest/trade_inception_present.md`（含组合收益、买卖次数及卖点日期）。买入信号日可用 `backtest_buy_signals.py --list-dates` 对照。

**可选：牛熊市场状态**（`MARKET_REGIME_ENABLED=false`，默认关）：基于中证1000/创业板/纳指的区间位置与 MA 斜率判定牛/熊/震荡，动态调整买入金额乘数与轮动门槛（见 `market_regime.py`）。

**实盘操作提示（美股）**：纳指信号约每 7 个交易日一次，跟投繁琐时可改为**周度合并执行**（例如周四一次性买入本周累计建议金额），成本影响通常有限，但能减轻操作负担。回测按信号日全额计入，**未模拟 QDII 日限购**。
---

## 文件说明

### 入口脚本

| 文件 | 作用 |
|------|------|
| `report.py` | **本地报告入口**。按模块拉取数据、评估信号、打印报告，不推送。 |
| `push.py` | **微信推送入口**。调用 `report.generate_reports()` 生成报告，经 Server酱 推送。 |
| `sync_market_duckdb.py` | **推荐**：统一同步公网缓存、策略数据、StockDB 到 DuckDB。 |
| `sync_data_cache.py` | 仅预拉/增量更新公网 CSV 缓存（不写入 DuckDB）。 |
| `backtest.py` | **统一回测入口**（`-m buy/trade/rotation/regime/wfa/optimize/inception`）。 |
| `backtest_buy_signals.py` 等 | 各模式实现模块（由 `backtest.py` 调用，也可直接运行）。 |

### 批处理

| 文件 | 作用 |
|------|------|
| `run_report.bat` | 本地运行 `report.py`。 |
| `run_push.bat` | 定时任务执行脚本：调用 `push.py`，输出追加到 `logs/push.log`。 |
| `run_sync_market_duckdb.bat` | 调用 `sync_market_duckdb.py`，日志写入 `logs/sync_market_duckdb.log`。 |
| `setup_task.bat` | 一键注册 Windows 计划任务（工作日 14:00 推送），并清理旧版单指数任务。 |
| `setup_sync_task.bat` | 注册每天 09:30 数据同步任务（`sync_market_duckdb.py`）。 |

### 研究 / 验证脚本（`scripts/`）

非日常入口，用于回测研究、因子消融、数据校验等：

| 文件 | 作用 |
|------|------|
| `scripts/monte_carlo_permutation.py` | 蒙特卡洛置换检验（判断回测是否显著优于运气） |
| `scripts/validate_data_baostock.py` | 数据源交叉验证（baostock / 国债 / ETF / 美股 PE） |
| `scripts/verify_duckdb.py` | DuckDB 数据完整性检查 |
| `scripts/monthly_rolling_backtest.py` | 红利低波策略滚动窗口回测 |
| `scripts/factor_ablation.py` | 因子消融实验 |
| `scripts/factor_builder.py` | 从本地 Parquet 数据湖构建长表因子集市 |
| `scripts/factor_loader.py` / `factor_loader.py` | 查询单因子或合并后的多因子长表 |
| `scripts/factor_snapshot_builder.py` | 按年并行构建、增量更新横截面因子宽表 |
| `scripts/factor_snapshot_loader.py` / `factor_snapshot_loader.py` | 单日或日期范围 Snapshot 查询接口 |
| `scripts/factor_snapshot_inspector.py` | 查看 Snapshot 日期范围、覆盖率、统计与同步状态 |
| `scripts/verify_snapshots.py` | 数据一致性与查询性能验收，生成 Markdown 报告 |
| `scripts/archive_output.bat` | 将 `output/` 回测结果归档到 `output/_archive/日期/` |

### 策略模块

每个投资标的拆分为「数据层」和「信号层」：

| 模块 | 数据层 | 信号/报告层 | 覆盖指数 |
|------|--------|-------------|----------|
| 红利 | `dividend_data.py` | `core.py` | H30269 |
| A股宽基 | `cn_broad_data.py` | `cn_broad_signal.py` | 000852、000688 |
| 创业板 | `cyb_data.py` | `cyb_signal.py` | 399006 |
| 美股 | `us_index_data.py` | `us_index_signal.py` | NDX、SPX |

- **数据层**：拉取行情/估值、构建历史面板、计算分位。
- **信号层**：按 `config/` 配置包中的阈值判定买入/观望/卖出，格式化报告段落。
- **`core.py`**：红利模块的信号与报告逻辑（`generate_report()` 供 `report.py` 调用）。

### 共享库

| 文件 | 作用 |
|------|------|
| `config/` | 配置包：指数列表、买卖阈值、环境变量读取、路径以及 `push.env` 加载。 |
| `index_meta.py` | 各指数基日与缓存/回测拉取范围。 |
| `data_cache.py` | 历史数据本地缓存（A 股 / 美股分目录，按日刷新）。 |
| `data_sources.py` | 数据源 URL 注册表与接口说明（中证、东财、FRED、新浪等）。**完整清单见 [`DATA_SOURCES.md`](DATA_SOURCES.md)**。 |
| `market_data.py` | 公共行情工具：国债收益率、中证历史行情、分位计算、UTF-8 输出。 |
| `realtime_quote.py` / `live_snapshot.py` | 盘中行情与实盘快照（叠加到报告）。 |
| `signal_format.py` | 统一信号文案：买入/观望/卖出标记、判定条件块、模块标题格式。 |
| `drop_to_buy.py` | 「再跌多少可触发买入」推演（含近1年区间位置，与回测一致） |
| `price_position.py` | 近1年区间位置、距低点涨幅、距高点回撤及分层阈值计算 |
| `sell_trailing.py` | 移动止盈（浮盈达标后自峰值回撤卖出） |
| `buy_amount_config.py` / `buy_amount_budget.py` / `buy_amount_change.py` | 买入金额：基准、年度预算、涨跌缩放 |
| `buy_amount_allocation.py` / `buy_amount_ranking.py` | 位置分配（默认）与收益率排名分配（对比用） |
| `rotation_sell.py` / `market_regime.py` | 轮动卖出门控与牛熊市场状态 |
| `backtest_rotation.py` / `backtest_metrics.py` | 组合轮动回测与风险指标 |
| `backtest_html.py` | 回测 HTML 图表 |
| `notify.py` | Server酱 微信推送封装，与报告生成解耦。 |

### 配置文件

| 文件 | 作用 |
|------|------|
| `push.env` | 实际配置（含 SendKey），**勿提交版本库**（已加入 `.gitignore`） |
| `push.example.env` | 配置模板，可复制为 `push.env`。 |

阈值默认值在 `config/` 各模块中，可通过 `push.env` 覆盖，详见 `push.example.env` 注释。

### 运行时目录

| 路径 | 作用 |
|------|------|
| `logs/` | **仅运行时日志**（如 `push.log`、`sync_cache.log`，由定时任务追加写入）。 |
| `cache/` | 外部数据本地缓存：A 股（`cache/cn/`）、创业板（`cache/cyb/`）、美股（`cache/us/`），及 `position_allocation.json`（当日买入额度分配），按日刷新。 |
| `output/backtest/` | 回测输出（`inception_present.md/html`、`trade_inception_present.md/html`）。 |
| `output/_archive/` | 历史回测输出归档（可安全删除后重跑回测再生）。 |
| `data/parquet/factors/` | 长表因子集市；由 `scripts/factor_builder.py` 构建。 |
| `data/parquet/factors/snapshots/` | 按交易日分区的横截面因子宽表及同步 manifest。 |
| `output/snapshot_validation_report.md` | `verify_snapshots.py --full` 生成的宽表验收报告。 |

---

### 价格位置指标（全模块共用）

买入侧除估值分位外，统一使用 **近 252 交易日（约 1 年）** 的价格位置过滤（见 `price_position.py`）：

| 指标 | 字段名 | 含义 |
|------|--------|------|
| 近1年区间位置 | `year_range_position` | 收盘价在近 252 日高低点区间中的位置（0=最低，1=最高）；买入须 ≤ `buy_max_year_range_pct` |
| 距低点涨幅 | `pct_above_low` | 收盘价 / 近 N 日低点 − 1（N 默认 252，见各指数 `buy_low_lookback_days`） |
| 距高点回撤 | `pct_below_high` | 1 − 收盘价 / 近 252 日高点 |

**分层规则**（`config/price_position.py` 全局默认，可通过 `push.env` 覆盖）：

- **近1年低位**（区间位置 ≤ `BUY_NEAR_YEAR_LOW_RANGE_PCT`，默认 20%）：放宽估值门槛（PE/利差/利率/PEG 等），豁免距高点回撤要求（仅当区间 ≤ `BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT`，默认 12%），并放宽距低点涨幅上限。
- **近1年中高位**（区间位置 > `BUY_MID_RANGE_POSITION_PCT`，默认 45%）：收紧距低点涨幅（上限降至 `BUY_MID_RANGE_MAX_ABOVE_LOW_PCT`，默认 6%），避免反弹途中追高。
- **硬性上限**：无论估值多便宜，区间位置超过 `buy_max_year_range_pct` 一律不买。
- **均线趋势**（`MA200` 斜率，默认 60 日变化率）：MA 斜率 ≥ `BUY_TREND_MIN_MA_SLOPE_PCT`（默认 -2%）视为企稳可买；若 MA 仍明显下行，仅当近1年区间 ≤ `BUY_TREND_DOWNTREND_MAX_RANGE_PCT`（默认 10%）才允许买入，避免熊市反弹途中追高。

各指数 `buy_max_year_range_pct` 示例：中证1000 **34%**、科创50 **38%**、创业板 **42%**、纳指100 / 标普500 **68%**。

**分指数独立变量**：宽基通过 `get_cn_broad_signal_config(code)`（`CN_BROAD_{代码}_*` 环境变量）；红利 `DIVIDEND_{代码}_*`；创业板/纳指/标普为 `CYB_*` / `NDX_*` / `SPX_*`（含 `BUY_TREND_*`、`BUY_MID_RANGE_*` 等）。默认值见 `config/cn_broad.py` 中 `_CN_BROAD_PER_INDEX_DEFAULTS` 与 `config/` 下各模块常量。

上述指标已接入：`report.py` 报告输出、`backtest_buy_signals.py` / `backtest_trade_signals.py` 回测、`drop_to_buy.py` 盘中推演。红利模块不使用 MA 趋势过滤（策略为股息率+利差，距低点口径为 90 日）。

趋势相关环境变量（见 `push.example.env`）：`BUY_TREND_MA_DAYS`、`BUY_TREND_SLOPE_LOOKBACK_DAYS`、`BUY_TREND_MIN_MA_SLOPE_PCT`、`BUY_TREND_DOWNTREND_MAX_RANGE_PCT`。

---

## 模块与策略概要

各指数完整买卖标准如下（默认阈值见 `config/`，可通过 `push.env` 覆盖）。**价格位置**指近 252 交易日（约 1 年）的区间位置、距低点涨幅、距高点回撤及 MA200 趋势过滤，详见上文「价格位置指标」。

### 红利（dividend）— 仅买入，无卖出

| 指数 | 代码 | 买入条件（须全部满足） |
|------|------|------------------------|
| 中证红利低波动 | H30269 | 利差 **> 2.8%**；利差分位 **≥ 40%**；PE 分位 **≤ 74%**；距 90 日低点 **≤ 6%**；近1年区间 **≤ 58%**；高点回撤 **≥ 10%**；股息可持续性（PE **≥ 5**；252 日股息率飙升 **≤ 1.5×**，近1年区间 **≤ 10%** 时豁免飙升检查） |

- **分位窗口**：利差分位约 3 年（756 交易日）；PE 分位同窗口。
- **近1年低位放宽**：区间位置 ≤ 20% 时，利差分位门槛 −10、PE 分位上限 +12，绝对利差门槛 −1.2%；区间位置 ≤ 5% 时可豁免绝对利差硬门槛；分位样本不足时允许仅凭价格位置买入。
- **卖出（实盘报告）**：无（长期持有型）。
- **卖出（回测）**：可选移动止盈（浮盈 **≥ 40%** 后自峰值回撤 **≥ 10%**），受轮动门控约束；可用 `DIVIDEND_SELL_ENABLED=false` 关闭。
- **股息可持续性**：拦截「股价暴跌推高股息率」（近 252 日股息率飙升 > 1.5×）与极端低 PE（< 5）。近1年区间 **≤ 10%**（`DIVIDEND_YIELD_SPIKE_WAIVE_RANGE_PCT`）时**豁免**股息率飙升检查（仍保留 PE ≥ 5），以免系统性崩盘黄金坑拒买；可用 `DIVIDEND_SUSTAINABILITY_ENABLED=false` 关闭整项。
- **回测收益**：使用中证全收益指数（H30269→H20269）估算，含分红再投资。

### A 股宽基（cn_broad）— 移动止盈

**买入逻辑**：股债利差分位、PE 分位、PB 分位（有数据时）等纳入评分，须 **多数指标 favorable**（`score ≥ max(3, total−1)` 且 `total ≥ 2`），且股债利差达标；同时须通过**价格位置硬过滤**（距低点、高点回撤、近1年区间、MA 趋势）。

**卖出逻辑**（两只宽基均启用移动止盈；**估值卖点已移除**）：

1. **分批 + 移动止盈**（科创50）：以**初始仓位**为基准分三档——浮盈 **50%** 卖 **1/3**、**80%** 再卖 **1/3**；剩余 **1/3** 进入移动止盈，自持仓峰值回撤 **≥ 12.5%** 清仓。
2. **移动止盈兜底**（中证1000）：浮盈 **≥ 40%** 后自峰值回撤 **≥ 10%** 清仓。
3. 卖出与买入互斥：当日若仍满足买入条件，不触发卖出。回测中卖点受**轮动门控**约束。

| 指数 | 代码 | 买入（分位窗口约 10 年） | 卖出 |
|------|------|--------------------------|------|
| 中证1000 | 000852 | 利差 **≥ 70%**；PE **≤ 52%**；PB **≤ 56%**；距低点 **≤ 5%**；近1年区间 **≤ 34%**；高点回撤 **≥ 16%** | 浮盈 **≥ 40%** 后回撤 **≥ 10%** |
| 科创50 | 000688 | 利差 **≥ 64%**；PE **≤ 52%**；PB **≤ 58%**；距低点 **≤ 7%**；近1年区间 **≤ 38%**；高点回撤 **≥ 14%** | 分批：50% 卖 **1/3**、80% 卖 **1/3**；余 **1/3** 移动止盈回撤 **≥ 12.5%** |

- **环境变量前缀**：`CN_BROAD_{代码}_*`。

### 创业板指（cyb）— 仅买入

| 方向 | 条件（须全部满足） |
|------|-------------------|
| **买入** | 加权 PE 分位 **≤ 46%**；加权 PB 分位 **≤ 38%**；PEG（近 5 年盈利 CAGR，自动滚动，不足时回退 **16.63%**）**≤ 2.2**；距 90 日低点 **≤ 6%**；近1年区间 **≤ 42%**；距 252 日高点回撤 **≥ 18%**；MA 趋势过滤 |
| **卖出** | 无（`cyb_signal.py` 当前不输出卖点；回测亦仅买） |

- **分位窗口**：PE/PB 约 10 年（2520 交易日）。
- **PEG 增速**：默认启用 `CYB_HISTORICAL_GROWTH_AUTO`：用日度 close/PE 隐含盈利估算滚动 5 年 CAGR（无未来函数）；样本不足时回退 `CYB_HISTORICAL_GROWTH`（16.63%）。自动值不低于 `CYB_HISTORICAL_GROWTH_FLOOR`（默认 **同为 16.63%**），即自动化主要在增速高于兜底时生效，避免盈利低谷把买入信号收没；若希望按真实低增速收紧，可将 floor 调低（如 `0.10`）。可用 `--growth` / `CYB_EXPECTED_GROWTH` 覆盖买入侧机构预期增速。
- **环境变量前缀**：`CYB_*`。

### 纳斯达克 100 / 标普 500（us）— 仅买入（报告）

| 指数 | 代码 | 买入条件（须全部满足） |
|------|------|------------------------|
| 纳斯达克 100 | NDX | Forward PE 分位 **≤ 87%**（无则 TTM **≤ 98%**）；PEG **≤ 2.5**（TTM 退化为 **≤ 1.5**）；10Y 利率分位 **≤ 99%**；21 日利率升幅 **≤ 40bp**；近1年区间 **≤ 68%**；MA 趋势过滤 |
| 标普 500 | SPX | 同上结构；Forward PE **≤ 87%**；PEG Forward **≤ 1.8** / TTM **≤ 1.45** |

- **PEG 增速回退**：显式配置 → 隐含增速 → 5 年历史增速 → 兜底（NDX 19%、SPX 10%）。详见下文「美股估值数据与 PEG 回退逻辑」。
- **利率斜率**：21 交易日美债升幅超过 40bp 时暂缓买入（防 2022 式利率急升）。
- **卖出（实盘报告）**：无（长期持有型）；建议通过场外 QDII 基金执行，注意限购。
- **卖出（回测）**：移动止盈（NDX 浮盈 **≥ 50%** 后回撤 **≥ 12%**；SPX 回撤 **≥ 12%**）+ 估值卖点（Forward PE 分位 **≥ 88%** 且距低点 **≥ 30%**），受轮动门控约束；可用 `US_INDEX_SELL_ENABLED=false` 关闭。
- **环境变量前缀**：`NDX_*` / `SPX_*`。

### 买卖标准速查

| 模块 | 指数数 | 买入核心 | 卖出核心（实盘报告 / 回测） |
|------|--------|----------|------------------------------|
| 红利 | 1 | 股息利差 + 利差/PE 分位 + 价格位置 | 无 / 移动止盈（轮动门控） |
| 宽基 | 2 | 股债利差分位 + PE/PB 分位 + 价格位置（评分制） | 移动止盈（分批或回撤） / 同左 + 轮动门控 |
| 创业板 | 1 | PE/PB 分位 + PEG(5年) + 价格位置 | 无 / 无 |
| 美股 | 2 | Forward PE + PEG + 利率分位 + 价格位置 | 无 / 移动止盈 + 估值（轮动门控） |

---

## 项目结构

```
投资推送/
├── report.py / push.py / sync_market_duckdb.py / sync_data_cache.py
├── core.py / dividend_data.py
├── cn_broad_data.py / cn_broad_signal.py
├── cyb_data.py / cyb_signal.py
├── us_index_data.py / us_index_signal.py
├── config/                # 配置包（indices / dividend / buy_amount / …）
├── backtest.py            # 统一回测入口
├── backtest_buy_signals.py / backtest_trade_signals.py / …
├── market_data.py / realtime_quote.py / live_snapshot.py
├── signal_format.py / drop_to_buy.py / price_position.py / sell_trailing.py
├── buy_amount_config.py / buy_amount_budget.py / buy_amount_change.py
├── buy_amount_allocation.py / buy_amount_ranking.py
├── rotation_sell.py / market_regime.py
├── index_meta.py / data_sources.py / data_cache.py
├── backtest_regime_compare.py / backtest_wfa.py / backtest_optimize.py
├── backtest_html.py / backtest_metrics.py
├── notify.py
├── factor_loader.py / factor_snapshot_loader.py  # 因子查询兼容入口
├── scripts/               # 研究、验证与因子集市工具
├── dividend_lowvol_rotation/  # 红利低波轮动策略子模块
├── run_report.bat / run_push.bat / run_sync_market_duckdb.bat
├── setup_task.bat / setup_sync_task.bat
├── push.example.env
├── .gitignore
├── push.env               # 本地配置（勿提交）
├── logs/                  # 运行时日志
├── cache/                 # 外部数据缓存（cn / cyb / us / position_allocation.json）
├── data/parquet/factors/  # 长表因子集市与 snapshots/ 宽表
└── output/                # 回测结果与 Snapshot 验收报告
```

---

## 开发说明

- 调试时**仅运行** `python report.py` 查看输出，避免频繁触发微信推送。
- 修改阈值优先改 `config/` 对应模块的默认值，或通过 `push.env` 覆盖。
- 数据源变更查阅 [`DATA_SOURCES.md`](DATA_SOURCES.md)（完整清单）与 [`data_sources.py`](data_sources.py)（URL 注册表）。

---

## 信号强度评分方法论

报告末尾「跨指数信号对比表」中的**强度分**由 `signal_scoring.py` 计算，用于横向比较各指数当日买入吸引力，**不替代**各模块自身的硬门槛判定。对比表含 **硬门槛** 列（✅/❌）：硬门槛未达标时，即使强度分较高，最终信号仍为观望。

| 维度 | 权重 | 含义 |
|------|-----:|------|
| 估值 | 40% | PE/PB/Forward PE/利差/股息率/利率分位等，分位越低（利差越高）得分越高 |
| 价格位置 | 30% | 近1年区间位置越低、距低点涨幅越小、距高点回撤越深得分越高 |
| 趋势 | 20% | MA200 斜率向上得分高；近年内低位给予保底分 |
| 就绪度 | 10% | 买入条件达标比例（`score/total`） |

**分级与可执行买入**（`signal_enrich.py`）：

| 强度 | 分级 | 可执行买入 |
|------|------|------------|
| ≥ 60 | 强 | 条件达标 → **买入** |
| 40–59 | 中 | 条件达标但强度偏低 → **可买** |
| < 40 | 弱 | **观望** |

（可选）若开启 `BUY_COOLDOWN_ENABLED`，强买入还需冷却期通过；关闭时冷却检查恒为通过。

「接近买入」：多数条件已达标且未达标项距阈值 ≤ 5 个百分点。

---

## 美股估值数据与 PEG 回退逻辑

### 数据来源

| 数据 | 来源 | 说明 |
|------|------|------|
| Forward / TTM PE | [historyofmarket.com](https://historyofmarket.com) JSON API | 月度序列，按日向前填充；本地缓存 `cache/us/*_forward_pe.json` |
| 指数价格 | FRED + akshare 新浪美股指数 | 日频 |
| 10Y 美债 | FRED `DGS10` + akshare 备用 | 日频 |

Forward PE 为**市场一致预期**的聚合值，非单一机构预测；接口异常时回测/报告可能仅有 TTM PE。

**当月数据时效**：Forward PE 为月度更新，当月尚未发布新值时，日频回测与实盘报告对当月交易日**沿用最近一次已发布月度值**（`merge_asof` 向后对齐，不偷看未来）。historyofmarket 新值通常在**次月中旬**才入库，因此 8 月初往往仍沿用 **6 月末** 数据，实际滞后约 **1–2 周至逾 1 个月**（取决于当月是否已发布）。接口异常时回退 TTM PE。

### PE 买入判定

1. 有 Forward PE 分位 → 使用 Forward PE 门槛
2. 否则 → 退化为 TTM PE 分位

### PEG 增速与回退链

`resolve_expected_growth()` 按以下顺序取盈利增速（小数）：

1. 命令行 `--us-growth` / 环境变量 `NDX_EXPECTED_GROWTH` / `SPX_EXPECTED_GROWTH`
2. **隐含增速**：`trailing_pe / forward_pe − 1`（需同时有 TTM 与 Forward）
3. **历史 5 年盈利增速**：由月度 Forward 盈利序列 CAGR 估算
4. **兜底**：NDX **19%**、SPX **10%**（`NDX_FALLBACK_EXPECTED_GROWTH` 等）

PEG 买入判定：

- 有 Forward PE → `PEG = forward_pe / (增速 × 100)`，对比 `NDX_BUY_PEG_FORWARD_MAX`（默认 2.5）
- 无 Forward → `PEG(TTM) = trailing_pe / (历史或兜底增速 × 100)`，对比 `NDX_BUY_PEG_HIST_MAX`（默认 1.5）

高增速（NDX > 20%）时 Forward PEG 上限 +0.2；近1年低位再放宽 +0.5。

**分析师乐观偏差**：PEG 常用隐含增速 `trailing_pe / forward_pe − 1`。市场顶部时分析师往往高估未来盈利，Forward PE 偏低 → 隐含增速偏高 → PEG 看似便宜，可能在高位误判低估。Forward PE 分位 ≤ 87% 意味着仍有约 13% 历史时点更贵，并非深度低估区。实盘宜关注盈利修正趋势；若一致预期下调，即便 Forward PE 分位不高也应谨慎加仓。

### 利率斜率过滤

除利率**分位** ≤ 99% 外，新增 **21 日利率升幅** ≤ 40bp（`NDX_BUY_RATE_MAX_SLOPE=0.004`）。2022 年式利率急升阶段即使分位不高也会拦截买入。

---

## 止盈策略：移动止盈 + 智能轮动

### 移动止盈（单指数）

高波动品种（科创50）采用**三档分批**；中证1000 以移动止盈回撤兜底：

| 品种 | 浮盈 50% | 浮盈 80% | 剩余仓位 |
|------|----------|----------|----------|
| 科创50 | 卖出初始仓位 1/3 | 再卖 1/3（80%） | 最后 1/3 移动止盈（回撤 12.5%） |
| 中证1000 | — | — | 浮盈 ≥40% 后回撤 ≥10% 清仓 |
| 红利（回测） | — | — | 浮盈 ≥40% 后回撤 ≥10% |
| 美股（回测） | — | — | 移动止盈回撤 ≥12%（另含估值卖点） |

实现见 `sell_trailing.simulate_trades_trailing`。创业板当前不启用卖点。

### 智能轮动（组合级，默认开启）

`ROTATION_SELL_ENABLED=true` 时，回测在组合层面模拟：

1. **共享资金池**：各指数卖出所得进入统一现金池，优先满足同日其他指数的买入
2. **轮动门控**：仅当同日**有其他指数触发买入**时才执行卖出；估值类卖点还需持仓年化 **< 门槛**（默认 10%，牛熊状态下可调）
3. **牛熊调节**（`MARKET_REGIME_ENABLED=false` 默认关）：牛市提高轮动门槛、略减买入；熊市降低门槛、略增买入

回测见 `backtest_trade_signals.py` → `trade_inception_present.md`；独立对比见 `backtest_rotation.py`。

**分批卖出后的行为**（当前实现）：

- 同一持仓内，各浮盈档位（50%/80%）**仅触发一次**（`stages_triggered` 记录）；不会在震荡中于同一档位反复卖出。
- 持仓未清零时继续买入，会**累加** `initial_units_at_position`，后续分批卖出按累计初始份额计算。
- **清仓后**下一笔买入视为新仓位，分批档位重置。
- **轮动门控**（`ROTATION_SELL_ENABLED`，**默认开**）：无其他指数买点时不卖出，避免资金闲置；可用 `ROTATION_SELL_ENABLED=false` 退回各指数孤立卖出模式。
- **止盈后再买入约束**（`SELL_REBUY_GATE_ENABLED`，**默认关**）：可选开启；开启后一旦某档分批止盈已触发（或峰值浮盈曾达首档），持仓浮盈须回落至 **≤ 30%**（`SELL_REBUY_MAX_GAIN_PCT`）才允许继续买入。与「无冷却」理念一致时保持关闭，靠估值/价格位置硬门槛控频。
- **买入冷却期**（`BUY_COOLDOWN_ENABLED`，**默认关**）：可选人为限制连续买入间隔；默认关闭后，买入次数完全由硬门槛决定。
- **牛熊市场状态**（`MARKET_REGIME_ENABLED`，**默认关**）：基于代理指数（默认 000852/399006/NDX）的区间位置与 MA 斜率判定，动态调整买入乘数与轮动门槛。

**组合再平衡**：系统不自动再平衡。买入金额由位置分配动态决定，智能轮动通过资金池复用提高资金效率；因各指数触发频率不同，持仓市值会漂移——请按年度或主观风险偏好**手动再平衡**。

---

## 实盘注意事项与策略风险

以下为框架外的执行层风险，代码**默认不处理**，需认知或人工干预：

| 风险 | 说明 |
|------|------|
| **仓位漂移** | 各指数触发频率不同，长期持仓市值权重会偏离主观目标；建议定期检视并手动再平衡。美股实盘受 QDII 日限约束，实际资金流入未必与信号次数成正比。 |
| **位置分配波动** | 买入金额随区间位置与就绪度每日变化，同一指数不同信号日的建议金额可能差异较大；属设计行为，非 bug。 |
| **红利仓位难买满** | 红利抗跌、牛市滞涨，买入信号相对稀少，「压舱石」可能长期缺位，账户波动高于名义 50% 权重预期。 |
| **分批止盈代价** | 50%/80% 分批卖出在超级大牛市中会踏空部分涨幅；这是用利润落袋换取震荡市保护的**策略代价**，非缺陷。 |
| **红利高股息陷阱** | 股息率飙升有时源于股价暴跌或周期顶部。已有 PE 下限 + 股息率飙升过滤；极端低位可豁免飙升检查以免错过黄金坑。 |
| **小额申购摩擦** | 基准单次偏低时，场外 C 类份额频繁申赎可能触发短期赎回费；建议合并资金、降低交易频率。 |
| **QDII 限购** | 纳指/标普信号频繁，但限购使实际流入受限；回测按信号全额买入、**未模拟日限购**，美股收益可能偏乐观。 |
| **美股估值滞后** | Forward PE 月度数据可能滞后 1–2 个月；近1年区间与 MA 趋势硬过滤可兜底极速拉升时的「假便宜」。 |
| **操作频率** | 美股小额高频适合限购环境，但手动跟投负担大；可周度合并执行而不必逐信号下单。 |

---

## 交易说明（场外基金）

本系统信号基于**指数估值与价格**，实际操作通常通过**场外指数基金 / ETF 联接基金**申购，而非直接买卖指数：

| 事项 | 说明 |
|------|------|
| 申赎确认 | T+1 或 T+2 确认，与信号日收盘价存在滞后 |
| 最低申购 | 多数联接基金 **10 元起**；部分 QDII（纳指/标普）常见 **1 元或 10 元** 起，以基金公司公告为准 |
| 限购 | QDII 常有限购额度（如每日 100 元），与信号金额可能不一致 |
| 费率 | 申购费、赎回费、管理费未计入回测收益 |

报告中的「买入金额」为**参考仓位**，请结合账户限额与流动性自行调整。

---

## 红利低波个股策略（`dividend_lowvol_rotation/`）

该子项目在全市场分红股中构建个人版红利低波组合，对标 H30269 的股息率与低波思路，并加入财务质量、行业、Beta 和市值约束。它只生成评估报告和回测结果，**不连接 QMT 或券商下单接口**，也不复用根目录 H30269 指数定投信号。

### 快速运行

```bash
# 实盘评估报告：候选池、风险项、目标组合和参考买入区间
python -m dividend_lowvol_rotation.report --capital 800000

# 原生 10 年回测
python -m dividend_lowvol_rotation.backtest --years 10

# 与 H30269 全收益指数对比，并可运行 WFA / 蒙特卡洛
python -m dividend_lowvol_rotation.backtest_validate --benchmark H30269 --years 10

# Windows：自动选择 rqalpha_env 或系统 Python
run_dividend_lowvol_backtest.bat --years 10

# RQAlpha 引擎回测及原生基准对比
run_rqalpha_backtest.bat --years 10 --capital 100000
python -m dividend_lowvol_rotation.rqalpha.compare_baseline --years 10
```

报告缓存位于 `cache/dividend_lowvol/`，输出统一写入 `output/dividend_lowvol/`。免费数据源和字段口径见 [`dividend_lowvol_rotation/data_sources.md`](dividend_lowvol_rotation/data_sources.md)。

### 当前默认策略

1. **样本池**：全市场有分红记录的 A 股，剔除 ST；股息率默认使用 `auto` 口径，在最近一次派息过旧时切换为近 365 天 TTM。
2. **基础硬过滤**：股息率默认不低于 3.09%，最近一次除息不超过 3 年；使用动态低波上限，并保留 ROE ≥ 8%、资产负债率 ≤ 70% 的绝对质量底线。
3. **风险评分**：ROE 波动、近 5 年分红连续性、30%–70% 支付率、行业中性负债率和利息保障倍数默认采用**软评分扣减**（`DLV_SOFT_RISK_SCORING_ENABLED=true`），不是全部硬剔除；诊断和留存规则可显式使用硬判定。
4. **排序与组合约束**：股息率优先、低波辅助；保留行业分散、Beta 分散和市值分层三类结构性约束。
5. **权重**：股息率加权，单股目标权重上限 8%；波动率超过目标时允许降低总仓位并保留现金。
6. **调仓**：实盘报告默认 `entry_anniversary`（建仓周年），原生与 RQAlpha 回测默认 `index_annual`（1 月 15 日后首个交易日）。
7. **调出**：默认 `index_rules`，股息率低于 0.5% 或留存硬条件不满足时调出；止盈、止损、紧急卖出和市场状态开关默认关闭。

默认结构性约束如下：

| 约束 | 默认值 |
|------|--------|
| 市值分层 | 大盘门槛 200 亿元；中小盘合计权重 ≤ 40% |
| 行业分散 | 单行业 ≤ 20%；防御行业合计 ≤ 45%；前三行业合计 ≤ 50% |
| Beta 分散 | 低 Beta（≤ 0.68）合计 ≥ 45%；高 Beta 合计 ≤ 81% |
| 持仓与权重 | Top 10；单股目标权重 ≤ 8% |
| 波动控制 | 组合年化波动目标 20%，默认启用 |

旧文档曾将所有排雷项描述为硬过滤，并称若干软因子已从代码删除；这与当前实现不符。当前准确口径以 `dividend_lowvol_rotation/config.py`、`scoring.py` 和 `risk_screening.py` 为准。

### 原生回测口径

| 维度 | 默认行为 |
|------|----------|
| 行情源 | RQAlpha bundle（`DLV_BACKTEST_PRICE_SOURCE=rqalpha`） |
| K 线 | 不复权；现金分红单独入账，避免与前复权重复计算 |
| 分红/送转 | bundle 派息日与送股事件；派息日预扣红利税 |
| 成交 | bundle 收盘价，`DLV_EXECUTION_AT_CLOSE=true` 时不加滑点 |
| 成本 | 佣金万 0.854、最低 5 元；卖侧印花税按历史日期切换 |
| NAV | 现金保留全精度，输出时按分计；调仓后持仓加现金计值 |

需要与旧 DuckDB 行情路径对照时：

```bat
set DLV_BACKTEST_PRICE_SOURCE=duckdb
python -m dividend_lowvol_rotation.backtest --years 10
```

### RQAlpha 双引擎验证

RQAlpha 适配层复用原生策略的选股、排雷、组合约束和调仓计划；RQAlpha 引擎负责 T+1、停牌、涨跌停、佣金和印花税，独立原生台账负责与 `backtest.py` 对齐现金、股数和逐日 NAV。

```text
选股/风险评分/组合约束
          │
          ▼
compute_rebalance_plan
     ┌────┴─────────────┐
     ▼                  ▼
原生 backtest       RQAlpha 执行引擎
     │                  │
     └────逐日 NAV/成交对比────┘
```

环境搭建与执行：

```bat
scripts\setup_rqalpha_env.bat
set RQALPHA_BUNDLE_PATH=D:\rqalpha\bundle
run_rqalpha_backtest.bat --years 10 --capital 100000
python -m dividend_lowvol_rotation.rqalpha.compare_baseline --years 10
```

`scripts/setup_rqalpha_env.bat` 创建 `rqalpha_env` 并下载约 3 GB 的 bundle；也可手动安装 `rqalpha pandas numpy`。主要输出：

| 文件 | 说明 |
|------|------|
| `output/dividend_lowvol/rqalpha_result.pkl` | RQAlpha `sys_analyser` 结果 |
| `output/dividend_lowvol/rqalpha_native_nav.csv` | 原生口径逐日 NAV |
| `output/dividend_lowvol/rqalpha_backtest.md/html` | RQAlpha 回测报告 |
| `output/dividend_lowvol/rqalpha_vs_native.md` | 共有交易日指标、逐日 NAV 与调仓成交差异 |

当前对齐目标是共有交易日 `max_daily_nav_gap = 0`，并要求调仓日 signed delta 一致。对比指标只在两边共有交易日上计算，避免序列起止日不同造成 CAGR 虚差。

### 红利策略验证工具

```bash
# 暴雷事件前是否已经出现风险标记，检查未来信息
python scripts/verify_risk_lookahead.py

# 三类结构性约束消融与参数扫描
python scripts/constraint_ablation.py --sample-windows 12
python scripts/constraint_param_sweep.py --sample-windows 12

# 月度滚动窗口与 Parquet / bundle 引擎一致性
python scripts/monthly_rolling_backtest.py
python scripts/verify_parquet_backtest.py --years 10 --capital 100000
```

主要配置集中在 `dividend_lowvol_rotation/config.py`：

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `DLV_TOP_N_BUY` | `10` | 持仓数量上限 |
| `DLV_BACKTEST_REBALANCE_MODE` | `index_annual` | 回测调仓日程 |
| `DLV_LIVE_REBALANCE_MODE` | `entry_anniversary` | 实盘报告调仓日程 |
| `DLV_SELL_MODE` | `index_rules` | 留存硬规则调出 |
| `DLV_MV_TIER_SMALL_MAX_WEIGHT` | `0.40` | 中小盘合计权重上限 |
| `DLV_BETA_MIN_LOW_FRAC` | `0.45` | 低 Beta 合计下限 |
| `DLV_VOL_TARGET_ENABLED` | `true` | 启用组合波动目标 |
| `DLV_BACKTEST_PRICE_SOURCE` | `rqalpha` | 原生回测行情源 |
| `RQALPHA_BUNDLE_PATH` | `D:\rqalpha\bundle` | bundle 目录 |
| `DLV_BACKTEST_DIVIDEND_CASH` | `true` | 现金分红模式 |
| `DLV_EXECUTION_AT_CLOSE` | `true` | 收盘价成交 |
| `DLV_DIVIDEND_TAX_ENABLED` | `true` | 启用红利税 |
| `DLV_BACKTEST_INITIAL_CAPITAL` | `100000` | 回测初始资金 |

### 红利策略目录

| 路径 | 作用 |
|------|------|
| `strategy.py` / `scoring.py` | 实盘流水线、筛选排序与组合约束 |
| `risk_screening.py` | 风险因子、软评分扣减和硬判定 |
| `index_retention.py` / `index_portfolio.py` | 指数式调出与目标权重 |
| `corporate_actions.py` / `dividend_tax.py` | 送转与红利税 |
| `prices.py` / `costs.py` | 行情源、成交价与交易成本 |
| `backtest.py` / `backtest_validate.py` | 原生回测、WFA、蒙特卡洛和基准对比 |
| `report.py` | 实盘评估报告 |
| `rqalpha/` | RQAlpha 策略、原生台账及对比工具 |

### 红利个股策略风险

- 高股息可能来自股价暴跌、周期顶部或不可持续派息；软评分降低风险，不代表排除财务造假。
- Top 10 组合集中度高，行业和 Beta 上限只能缓解，不能消除个股尾部风险。
- 默认单股 8% 上限和波动目标可能使组合长期保留现金，不能把未满仓误判为构建失败。
- RQAlpha bundle、StockDB、财报与行业数据发布时间不同；未来信息验证通过不等于所有数据源都没有修订偏差。
- 回测按历史成交与税费模型估算，不代表实盘可按相同价格、容量和流动性成交。

---

## 共享 Parquet 数据基础设施（`data/parquet/`）

本地 Parquet 数据湖将 StockDB 日 K 与 RQAlpha bundle 的交易日历、涨跌停、分红送转、停牌和 ST 等元数据合并，供研究脚本、因子集市及 RQAlpha 数据源使用；它不依赖 `market.duckdb`。

```text
StockDB（日 K、估值、股票列表）       RQAlpha bundle（事件、日历、指数）
                  └──────────┬──────────┘
                             ▼
                scripts/export_to_parquet.py
                             ▼
                      data/parquet/
               ┌─────────────┼──────────────┐
               ▼             ▼              ▼
        研究查询/因子集市  ParquetDataSource  DuckDB/Polars
```

### 数据湖目录与口径

```text
data/parquet/
├── sync_manifest.json
├── stock_meta/                 # instruments、securities、stock_codes
├── trade_calendar/
├── stock_daily/year=YYYY/      # 不复权日 K、估值、涨跌停
├── stock_daily_qfq/year=YYYY/  # 前复权 close
├── index_daily/year=YYYY/      # 000001、000300
├── stock_dividend/
├── stock_split/
├── stock_suspended/
├── stock_st/
├── ex_cum_factor/
└── factors/                    # 长表因子及 snapshots/ 横截面宽表
```

- `stock_daily` 以 StockDB 的 OHLCV、估值和 `is_st` 为主，使用 RQAlpha 补充涨跌停以及仅 bundle 存在的交易日；每个 `(trade_date, symbol)` 应只有一行。
- 股票 `symbol` 始终是 6 位代码，不使用 `.XSHG/.XSHE` 后缀。`index_daily` 的 `000001`、`000300` 是上交所指数，导出时必须使用显式 `INDEX_ORDER_BOOK_IDS`，不能套用股票代码映射。
- `--scope all` 包含 StockDB 分类 `0/1/3/5/6/9`；`--scope a_share` 仅包含 `0/3/6/9`。ETF、LOF、REIT 可归档到日 K，但不保证 RQAlpha 可交易。
- Hive 分区格式为 `{domain}/year=YYYY/part_NNNN.parquet`；每批默认约 500 个代码。

`stock_daily` 主要字段：

| 字段组 | 字段 |
|--------|------|
| 主键 | `trade_date`, `symbol` |
| 行情 | `open`, `high`, `low`, `close`, `pre_close`, `volume`, `amount` |
| 衍生行情 | `turnover`, `pct_chg`, `amplitude`, `vol_ratio` |
| 估值/规模 | `pe_ttm`, `pb`, `total_mv`, `float_mv`, `total_share`, `float_share` |
| 状态 | `is_st`, `limit_up`, `limit_down`, `data_source` |

### 数据湖同步与断点恢复

```bat
REM 默认全范围增量同步
scripts\sync_parquet.bat --resume

python scripts\export_to_parquet.py --scope all --resume --start 2000-01-01

REM 仅 A 股
python scripts\export_to_parquet.py --scope a_share --resume

REM 强制重导 bundle 元数据
python scripts\export_to_parquet.py --force-meta
```

前置条件是 StockDB `127.0.0.1:7899` 可用，并已准备 RQAlpha bundle。`sync_manifest.json` 是断点账本，不是行情数据：

- `stock_daily[symbol].status == "ok"` 时，`--resume` 整只跳过，不拉取也不重写分区。
- `empty` 表示该代码无日 K，不创建数据分区。
- 判断是否就绪应以当前 `stock_codes.json` 中所有代码是否都进入 `stock_daily ok` 为准，不能直接比较 manifest 的历史累计键数。
- 当前恢复粒度是 symbol；若未来改为按年度重导，必须处理旧、新 `part_*` 重叠造成的重复主键。

快速查询：

```sql
SELECT *
FROM read_parquet(
  'data/parquet/stock_daily/year=*/part_*.parquet',
  hive_partitioning=true
)
WHERE symbol = '600000'
  AND trade_date >= '2020-01-01'
ORDER BY trade_date;
```

### 数据湖验证与 RQAlpha 数据源

```bash
python scripts/verify_parquet_backtest.py --years 10 --capital 100000
```

该命令比较 bundle 与 `ParquetDataSource` 的红利策略逐日 NAV，报告写入 `output/verify_parquet_vs_bundle.md`。相关入口：

| 路径 | 作用 |
|------|------|
| `scripts/export_to_parquet.py` | 导出、`--scope` 与 `--resume` |
| `scripts/sync_parquet.bat` | Windows 同步入口 |
| `data/parquet_constants.py` | 路径与 Schema 常量 |
| `data/parquet_loader.py` | 按 symbol 查询，不回退 StockDB |
| `data/parquet_data_source/` | RQAlpha `ParquetDataSource` |
| `dividend_lowvol_rotation/rqalpha/mod_parquet_data.py` | 将数据源注入 RQAlpha |

| 环境变量 | 说明 |
|----------|------|
| `DLV_PARQUET_ROOT` | 覆盖 Parquet 根目录 |
| `DLV_RQALPHA_DATA_SOURCE` | RQAlpha 引擎数据源：`bundle` 或 `parquet` |
| `DLV_RQALPHA_NATIVE_NAV_PATH` | 自定义原生口径 NAV 输出路径 |
| `DLV_BACKTEST_PRICE_SOURCE` | 策略台账 K 线来源；与引擎数据源是独立开关 |

数据湖当前不包含分钟线、期货或期权；bundle 的 `instruments.pkl` 也不覆盖 StockDB 的全部 ETF 等代码。

# AlphaPurify 因子诊断

项目通过 `alphapurify_bridge` 接入官方 `alphapurify==1.0.6`，避免本地包遮蔽第三方库。诊断层直接读取因子 Snapshot 与 `stock_daily`，输出 IC/IR、分层收益、因子收益归因、MD/HTML 报告，以及供 VectorBT 自动读取的批准因子清单。

```powershell
# 单因子诊断
.\.venv-vectorbt\Scripts\python.exe scripts\run_factor_diagnosis.py --factor dividend_yield --start 2010-01-01 --report

# 全因子诊断
.\.venv-vectorbt\Scripts\python.exe scripts\run_factor_diagnosis.py --all --report

# 验收（快速 / 十年全量）
.\.venv-vectorbt\Scripts\python.exe scripts\verify_factor_diagnosis.py
.\.venv-vectorbt\Scripts\python.exe scripts\verify_factor_diagnosis.py --full

# 要求 VectorBT 在批准清单存在后才运行
.\.venv-vectorbt\Scripts\python.exe scripts\run_vectorbt_pipeline.py --require-diagnosis
```

默认采用与官方 AlphaPurify 默认口径一致的 Spearman Rank IC。`--official` 会额外运行官方 AlphaPurify 抽检，计算开销较高。因子方向、阈值与预测期位于 `config/alphapurify/`；稳定交接文件为 `output/alphapurify/approved_factors.json`。
