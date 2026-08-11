# A 股红利低波轮动

复刻 [EasyXT](https://github.com/quant-king299/EasyXT) 红利低波五层筛选，**仅评估与回测**，不接入 QMT / 券商。

## 快速运行

```bash
# 实盘评估报告
python -m dividend_lowvol_rotation.report --top 3 --capital 80000

# 长周期回测（默认近 5 年，每 20 个交易日调仓）
python -m dividend_lowvol_rotation.backtest --top 10 --capital 80000 --years 5

# WFA + 蒙特卡洛（默认 bootstrap，约 1 次数据加载）
python -m dividend_lowvol_rotation.backtest_validate --wfa --monte-carlo --top 10 --years 5

# 对比指数 H30269（全收益 H20269，含分红再投资）
python -m dividend_lowvol_rotation.backtest_validate --benchmark H30269 --years 10 --top 10

# 置换调仓日蒙特卡洛（慢，慎用）
python -m dividend_lowvol_rotation.backtest_validate --monte-carlo --mc-method rebalance --permutations 50

# 或
run_dividend_lowvol.bat --top 3 --capital 80000
run_dividend_lowvol_backtest.bat --start 2018-01-01 --rebalance-days 20 --top 3
```

## 功能清单

| 功能 | 说明 | 配置 |
|------|------|------|
| 申万一级行业 | `index_component_sw` 成分映射，失败降级证监会 | `DLV_INDUSTRY_SOURCE=sw_fallback` |
| TTM 分红 | 近 12 个月累计派息；`auto` 在空窗期自动切换 | `DLV_DIVIDEND_YIELD_MODE=auto` |
| 动态阈值 | 股息率 ≥ 国债+利差；波动上限 = 池中位×倍数 | `DLV_DYNAMIC_THRESHOLD_ENABLED=true` |
| 动态权重 | 利率高加重红利；波动高加重低波 | `DLV_DYNAMIC_WEIGHT_ENABLED=true` |
| 长周期回测 | fhps 历史分红 + Baostock 前复权价 + 分红个税 | `backtest.py` |
| WFA / 蒙特卡洛 | 样本外窗口 + 收益自助抽样（秒级） | `backtest_validate.py` |
| 小资金/佣金 | 可配持仓数、万 0.854 最低 5 元 | `--top` / `DLV_COMMISSION_RATE` |

## 小资金示例

```bash
# 买 3 只，跌出前 6 调仓；8 万资金佣金估算
python -m dividend_lowvol_rotation.report --top 3 --capital 80000
```

`--sell-rank` 省略时 = `top × 2`。

## 主要环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `DLV_TOP_N_BUY` | 20 | 买入只数 |
| `DLV_DIVIDEND_YIELD_MODE` | auto | latest / ttm / auto |
| `DLV_DYNAMIC_THRESHOLD_ENABLED` | true | 动态股息率/波动门槛 |
| `DLV_DYNAMIC_WEIGHT_ENABLED` | true | 动态因子权重 |
| `DLV_INDUSTRY_SOURCE` | sw_fallback | sw / csrc / sw_fallback |
| `DLV_BACKTEST_REBALANCE_DAYS` | 20 | 回测调仓周期（交易日） |
| `DLV_DIVIDEND_TAX_ENABLED` | true | 回测扣分红个税（持股期限差异化） |
| `DLV_FHPS_REPORT_DATES` | 见 config | fhps 报告期批次（回测起点更早需含更多年份） |
| `DLV_PORTFOLIO_CAPITAL_CNY` | 0 | 资金量（佣金估算） |

完整列表见 `config.py`。

## 目录结构

```
dividend_lowvol_rotation/
  config.py
  dividend.py        # fhps + TTM
  dividend_tax.py    # 分红个税
  industry.py        # 申万一级
  dynamic_params.py  # 动态阈值/权重
  backtest.py        # 长周期回测
  backtest_validate.py  # WFA + 蒙特卡洛
  backtest_report.py    # MD/HTML 报告
  strategy.py / scoring.py / report.py
  costs.py / fundamentals.py / prices.py / quotes.py
```

缓存：`cache/dividend_lowvol/`；回测输出：`output/dividend_lowvol/`。

## 分红个税（回测）

使用前复权价（分红已体现在价格中），回测额外扣除**税负拖累**：

| 持股期限 | 实际税负 |
|----------|----------|
| ≤1 个月 | 20% |
| 1 个月～1 年 | 10% |
| >1 年 | 0% |

持股天数 = 买入日至除权日。关闭：`--no-dividend-tax` 或 `DLV_DIVIDEND_TAX_ENABLED=false`。

## 数据源

详见 [data_sources.md](./data_sources.md)。
