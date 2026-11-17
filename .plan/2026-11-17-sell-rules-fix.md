---
status: approved
human_confirmed: true
approved_sha256: de758ba4b1cd0db93c8d4be30de5db2bf7b26f5a7d666d369d2e7277ae127a8a
approved_version: 3
review_notes: v3 PASS 82/100；测试与优化脚本引用范围已全仓核查闭合并写入实施方案
---
# 修正创业板与红利低波卖出规则口径

## 前因后果
用户确认创业板应关闭卖出（仅科创50保留）、红利底仓不设卖点；但回测面板两指数分别出现 45/15 个卖出点。根因是创业板轮动分支硬编码启用卖出、红利全局卖出开关默认开启，与 config/dividend.py:22「不设卖点」矛盾。

## 现状与证据
- config/backtest_risk.py:10 CN_BROAD_SELL_ENABLED_CODES={"000688"}（399006 走该路径本已关闭）；:11 DIVIDEND_SELL_ENABLED 默认 True；:50-52 dividend_sell_enabled 忽略指数代码。
- backtest_rotation.py:155-175 创业板分支硬编码 has_sell=True 并挂估值分档卖出。
- valuation_trend_sell.py:51-90 创业板 PE 80/85/90 分档与 MA 破位；:118-143 红利利差/股息率分档卖出。
- backtest_trade_signals.py:194-207 红利实际走 tier 分档，:60-68 的移动止盈参数为死配置。
- 展示链路：backtest_trade_signals.py:985-986 rotation 缓存优先返回；backtest_buy_signals.py:966-981 注入图表。
- config/dividend.py:22 明确「仅配置买入阈值，不设卖点」。

## 实施方案（终稿 v3，用户已确认范围与兜底；2026-11-17 14:55 全仓符号核查已闭合）
引用点全集（grep 核实，无遗漏）：backtest_rotation.py:17-18/104/119/126-127；backtest_trade_signals.py:26/194-211/743-745/1017-1019/1188；config/backtest_risk.py:11/50-52；scripts/monte_carlo_permutation.py:505-506/518/528/542-543。tests/test_valuation_trend_sell.py 仅引用 valuation_trend_sell.dividend_sell_tiers（该函数保留不删），测试文件无需改动、不进 diff。无其他优化脚本引用红利卖出符号。

1. 移除红利类指数卖出逻辑代码（用户确认：红利相关逻辑均移除，非仅翻开关）：
   - backtest_rotation.py:17-18/104/119-127：删除 _dividend_trailing_cfg/_dividend_valuation_sell_fn/dividend_sell_enabled 导入与 sell_on 判定，has_sell 恒 False、trailing_cfg/valuation_sell_fn 恒 None。
   - backtest_trade_signals.py:26/194-211：删除导入与 _dividend_tier_fn/_dividend_trailing_cfg/_dividend_valuation_sell_fn；:743-745/754-756 改 sell_on=False、cfg=None；:1017-1019 分支删除；:1188 sell_dates_fn=None。
   - config/backtest_risk.py:50-52 dividend_sell_enabled 改恒返回 False 并注明遗留；:11/:60-68 参数块补遗留注释（valutation_trend_sell.dividend_sell_tiers 保留不删，测试引用仍在）。
   - scripts/monte_carlo_permutation.py:505-506/518/528/542-543：删除红利 cfg 引用，sell_on 恒 False（红利不再参与卖出回测）。
2. 创业板（rotation 既有硬编码）：
   - backtest_rotation.py:155-175：has_sell=True 改 False，trailing_cfg/valuation_sell_fn 置 None，不挂 _valuation_tier_fn。
3. rotation 缓存兜底（用户确认：按指数校验+未知保留）：
   - backtest_trade_signals.py:985-986：缓存命中前按指数校验——000688 走 cn_broad_sell_enabled（True 保留）、其余已知红利路径恒 False 返回 []、未知指数保留缓存原样。

## 验证计划
- pytest tests -k "rotation or trade or valuation" 通过（测试文件无需改动）。
- 重跑 python backtest.py --mode buy，断言 ALL_DATA 中 399006 sells==[]、H30269 sells==[]、000688 sells 非空、NDX 不变。
- git diff 仅含 backtest_rotation.py、backtest_trade_signals.py、config/backtest_risk.py、scripts/monte_carlo_permutation.py 四文件。
