#!/usr/bin/env python
"""Build the checked-in VectorBT workbench notebooks from concise cell definitions."""

from __future__ import annotations

from pathlib import Path

import nbformat as nbf


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOKS = ROOT / "notebooks"

BOOTSTRAP = """from pathlib import Path
import sys

start = Path.cwd().resolve()
ROOT = next((candidate for candidate in (start, *start.parents) if (candidate / 'vbt').exists()), start)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
print(f'项目根目录: {ROOT}')"""


def md(text: str):
    return nbf.v4.new_markdown_cell(text.strip())


def code(text: str):
    return nbf.v4.new_code_cell(text.strip())


def notebook(title: str, cells):
    book = nbf.v4.new_notebook()
    book.cells = [md(f"# {title}"), *cells]
    book.metadata.update(
        {
            "kernelspec": {"display_name": "Python (.venv-vectorbt)", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.10"},
        }
    )
    return book


BOOKS = {
    "00_Quick_Start.ipynb": notebook(
        "00 · VectorBT 快速入门",
        [
            md("依次运行全部单元格。默认用 2025 上半年快速验证完整生产规则；确认环境后可把日期改为十年区间。"),
            code(BOOTSTRAP),
            md("## 1. 环境检查"),
            code("""import vectorbt, pandas, numpy, pyarrow, duckdb
print({'vectorbt': vectorbt.__version__, 'pandas': pandas.__version__, 'numpy': numpy.__version__})"""),
            md("## 2. 加载配置与数据"),
            code("""from vbt.config import load_backtest_config, load_strategy_config
from vbt.adapters import VBTDataLoader

config = load_backtest_config({'start_date': '2025-01-01', 'end_date': '2025-06-30'})
params = load_strategy_config()
data = VBTDataLoader(start_date=config['start_date'], end_date=config['end_date']).load_aligned()
data.metadata"""),
            md("## 3. 运行完整规则回测"),
            code("""from vbt.engine import VBTEngine, PerformanceCalculator, ReportGenerator
from vbt.strategies import DividendLowVolStrategy

engine = VBTEngine(data=data, strategy=DividendLowVolStrategy(params), initial_capital=config['initial_capital'],
    commission=config['commission'], min_commission=config['min_commission'],
    stamp_duty_before=config['stamp_duty_before_2023_08_28'], stamp_duty_after=config['stamp_duty_after_2023_08_28'],
    slippage=config['slippage'], backtest_config=config)
results = engine.run()
perf = PerformanceCalculator(results)
perf.compute_metrics()"""),
            md("## 4. 图表与持仓"),
            code("""import pandas as pd
display(results.nav.to_frame('组合资产').plot(figsize=(12, 4), title='VectorBT 组合净值').get_figure())
display(ReportGenerator(results, perf, params).current_holdings())"""),
            md("## 5. 一键归档"),
            code("""paths = ReportGenerator(results, perf, params).archive(config['output_dir'])
paths"""),
        ],
    ),
    "01_Data_Exploration.ipynb": notebook(
        "01 · 因子数据探索",
        [
            code(BOOTSTRAP),
            md("## 1. 加载 Snapshot 与补齐字段"),
            code("""from vbt.adapters import DEFAULT_FACTORS, VBTDataLoader
loader = VBTDataLoader(start_date='2024-01-01', end_date='2024-12-31')
data = loader.load(factors=DEFAULT_FACTORS)
data.metadata"""),
            md("## 2. 因子覆盖率与缺失模式"),
            code("""import pandas as pd
coverage = pd.DataFrame({name: matrix.notna().mean(axis=1) for name, matrix in data.items() if name in DEFAULT_FACTORS})
display(coverage.describe().T)
coverage.plot(figsize=(12, 4), title='每日因子覆盖率')"""),
            md("## 3. 最新截面分布"),
            code("""import matplotlib.pyplot as plt
latest = pd.DataFrame({name: data[name].iloc[-1] for name in DEFAULT_FACTORS})
latest.hist(figsize=(14, 9), bins=40)
plt.tight_layout()"""),
            md("## 4. 相关性与箱线图"),
            code("""import seaborn as sns
fig, axes = plt.subplots(1, 2, figsize=(15, 5))
sns.heatmap(latest.corr(method='spearman'), annot=True, cmap='RdBu_r', center=0, ax=axes[0])
sns.boxplot(data=latest, orient='h', ax=axes[1])
plt.tight_layout()"""),
        ],
    ),
    "02_Single_Backtest.ipynb": notebook(
        "02 · 单次完整回测",
        [
            code(BOOTSTRAP),
            md("## 1. 配置与数据（默认十年生产对齐口径）"),
            code("""from vbt.config import load_backtest_config, load_strategy_config
from vbt.adapters import VBTDataLoader
config = load_backtest_config()
params = load_strategy_config()
data = VBTDataLoader(start_date=config['start_date'], end_date=config['end_date']).load_baseline_aligned(
    config['baseline_path'], initial_capital=config['initial_capital'])
config"""),
            md("## 2. 回测与绩效"),
            code("""from vbt.engine import VBTEngine, PerformanceCalculator, ReportGenerator
from vbt.strategies import DividendLowVolStrategy
engine = VBTEngine(data=data, strategy=DividendLowVolStrategy(params), initial_capital=config['initial_capital'],
    commission=config['commission'], min_commission=config['min_commission'], stamp_duty_before=config['stamp_duty_before_2023_08_28'],
    stamp_duty_after=config['stamp_duty_after_2023_08_28'], slippage=config['slippage'], backtest_config=config)
results = engine.run()
perf = PerformanceCalculator(results)
perf.compute_metrics()"""),
            md("## 3. 净值、回撤与持仓热力图"),
            code("""import matplotlib.pyplot as plt
import seaborn as sns
nav = results.nav
fig, axes = plt.subplots(2, 1, figsize=(13, 7), sharex=True)
nav.plot(ax=axes[0], title='组合资产')
(nav / nav.cummax() - 1).plot(ax=axes[1], title='回撤', color='firebrick')
plt.tight_layout()
active = results.positions.loc[:, results.positions.max().gt(0)].resample('ME').last()
plt.figure(figsize=(14, max(4, len(active.columns) * .22)))
sns.heatmap(active.T, cmap='YlGnBu', vmin=0)
plt.title('月末持仓权重热力图')"""),
            md("## 4. 导出 Markdown、HTML 与 Parquet"),
            code("ReportGenerator(results, perf, params).archive(config['output_dir'])"),
        ],
    ),
    "03_Parameter_Scan.ipynb": notebook(
        "03 · 参数扫描",
        [
            code(BOOTSTRAP),
            md("参数扫描走纯矩阵研究路径，可多核并行；最佳参数仍需在完整规则/RQAlpha 中复核。"),
            md("## 1. 数据与参数网格"),
            code("""from vbt.config import load_backtest_config, load_scan_config, load_strategy_config
from vbt.adapters import DEFAULT_FACTORS, VBTDataLoader
config = load_backtest_config()
scan_config = load_scan_config()
params = load_strategy_config({'alignment_mode': False})
data = VBTDataLoader(start_date=config['start_date'], end_date=config['end_date']).load(factors=DEFAULT_FACTORS)
scan_config"""),
            md("## 2. 并行运行"),
            code("""from vbt.engine import VBTEngine
from vbt.engine.parameter_scan import ParameterScan
from vbt.strategies import DividendLowVolStrategy
engine = VBTEngine(data=data, strategy=DividendLowVolStrategy(params), initial_capital=config['initial_capital'], backtest_config=config)
scan_results = ParameterScan(engine=engine, param_grid=scan_config['param_grid'], metric=scan_config['metric']).run(scan_config['n_jobs'])
display(scan_results.table.head(scan_config['top_k']))
scan_results.best_params()"""),
            md("## 3. 热力图与平行坐标"),
            code("""import seaborn as sns
import matplotlib.pyplot as plt
table = scan_results.table.query("status == 'ok'")
annual = table[table['rebalance_freq'].eq('A')]
if not annual.empty:
    sns.heatmap(annual.pivot(index='top_n', columns='volatility_60d_max', values=scan_config['metric']), annot=True, fmt='.2f')
    plt.title('年度调仓参数热力图')
import plotly.express as px
import plotly.io as pio
from IPython.display import HTML
parallel = px.parallel_coordinates(table, dimensions=['top_n', 'volatility_60d_max', 'annual_return', 'max_drawdown', 'sharpe_ratio'], color='sharpe_ratio')
display(HTML(pio.to_html(parallel, full_html=False, include_plotlyjs=True)))"""),
            md("## 4. 导出"),
            code("""from datetime import datetime
path = ROOT / 'output/vectorbt/param_scans' / f"notebook_scan_{datetime.now():%Y%m%d_%H%M%S}.parquet"
scan_results.to_parquet(path)"""),
        ],
    ),
    "04_Factor_Combination.ipynb": notebook(
        "04 · 因子组合研究",
        [
            code(BOOTSTRAP),
            md("## 1. 加载因子与未来收益"),
            code("""from itertools import combinations
import numpy as np
import pandas as pd
from vbt.adapters import DEFAULT_FACTORS, VBTDataLoader
data = VBTDataLoader(start_date='2020-01-01', end_date='2024-12-31').load(factors=DEFAULT_FACTORS)
future_return = data['close'].pct_change(20).shift(-20)
candidate_factors = list(DEFAULT_FACTORS)
candidate_factors"""),
            md("## 2. 单因子 Rank IC 与相关性"),
            code("""ic = pd.DataFrame({name: data[name].corrwith(future_return, axis=1, method='spearman') for name in candidate_factors})
summary = pd.DataFrame({'IC均值': ic.mean(), 'IC标准差': ic.std(), 'ICIR': ic.mean() / ic.std()}).sort_values('ICIR', ascending=False)
display(summary)
latest = pd.DataFrame({name: data[name].iloc[-1] for name in candidate_factors})
display(latest.corr(method='spearman'))"""),
            md("## 3. 遍历二/三因子等权 Rank 组合"),
            code("""rows = []
for size in (2, 3):
    for names in combinations(candidate_factors, size):
        daily_ic = ic[list(names)].mean(axis=1)
        rows.append({'factors': '+'.join(names), 'IC': daily_ic.mean(), 'ICIR': daily_ic.mean() / daily_ic.std()})
ranking = pd.DataFrame(rows).sort_values('ICIR', ascending=False)
display(ranking.head(20))"""),
            md("## 4. 导出组合排名"),
            code("""from datetime import datetime
path = ROOT / 'output/vectorbt/param_scans' / f"factor_combinations_{datetime.now():%Y%m%d_%H%M%S}.parquet"
path.parent.mkdir(parents=True, exist_ok=True)
ranking.to_parquet(path, index=False)
path"""),
        ],
    ),
    "05_Compare_with_RQAlpha.ipynb": notebook(
        "05 · 与 RQAlpha 对齐验证",
        [
            code(BOOTSTRAP),
            md("默认读取 `backtest_config.yaml` 中的十年 Parquet/RQAlpha 基准，执行正式验收。"),
            md("## 1. 运行自动验收"),
            code("""import subprocess
from vbt.config import load_backtest_config
baseline = ROOT / load_backtest_config()['baseline_path']
command = [sys.executable, str(ROOT / 'scripts/verify_vectorbt_vs_rqalpha.py'), '--baseline', str(baseline)]
completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
print(completed.stdout)
if completed.returncode:
    print(completed.stderr)
completed.check_returncode()"""),
            md("## 2. 查看对比报告"),
            code("""from IPython.display import Markdown, display
report_path = ROOT / 'output/vectorbt/reports/validation_report.md'
display(Markdown(report_path.read_text(encoding='utf-8')))"""),
        ],
    ),
    "templates/backtest_template.ipynb": notebook(
        "VectorBT 回测研究模板",
        [
            code(BOOTSTRAP),
            md("## 研究假设\n\n在这里记录参数变更的理由、预期影响和验收阈值。"),
            md("## 配置"),
            code("""from vbt.config import load_backtest_config, load_strategy_config
config = load_backtest_config()
params = load_strategy_config()
# 仅在这里覆盖参数，确保实验可复现：
overrides = {}
params.update(overrides)
config, params"""),
            md("## 数据、回测与结果"),
            code("""# 参考 02_Single_Backtest.ipynb；完成实验后将报告归档到 output/vectorbt/。"""),
            md("## 结论\n\n记录是否接受参数变更，以及与基准的差异。"),
        ],
    ),
}


def main() -> int:
    for relative, book in BOOKS.items():
        target = NOTEBOOKS / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        nbf.write(book, target)
        print(target.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
